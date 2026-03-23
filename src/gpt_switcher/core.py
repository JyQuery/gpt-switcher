from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1
REQUEST_MESSAGE_PREFIX = 'Request: "GET /backend-api/codex/responses HTTP/1.1'
WS_EVENT_PREFIX = "websocket event: "
RECEIVED_MESSAGE_PREFIX = "Received message "
DEFAULT_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR = "CODEX_REFRESH_TOKEN_URL_OVERRIDE"
REFRESH_TOKEN_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "gpt-switcher"

ACCOUNT_ID_PATTERN = re.compile(r"chatgpt-account-id:\s*(.+?)(?:\\r\\n|[\r\n]|$)")
OTEL_ACCOUNT_ID_PATTERN = re.compile(r'user\.account_id="([^"]+)"')
SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class SwitcherError(RuntimeError):
    pass


class LiveQuotaUnauthorizedError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppPaths:
    codex_home: Path
    switcher_home: Path

    @classmethod
    def from_env(cls) -> "AppPaths":
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        switcher_home = Path(os.environ.get("GPT_SWITCHER_HOME", "~/.gpt-switcher")).expanduser()
        return cls(codex_home=codex_home, switcher_home=switcher_home)

    @property
    def auth_path(self) -> Path:
        return self.codex_home / "auth.json"

    @property
    def logs_path(self) -> Path:
        return self.codex_home / "logs_1.sqlite"

    @property
    def state_path(self) -> Path:
        return self.codex_home / "state_5.sqlite"

    @property
    def registry_path(self) -> Path:
        return self.switcher_home / "registry.json"

    @property
    def accounts_dir(self) -> Path:
        return self.switcher_home / "accounts"


@dataclass(frozen=True)
class AuthMetadata:
    account_id: str
    email: str
    plan_type: str
    user_id: str | None
    auth_mode: str | None
    token_expires_at: int | None
    token_issued_at: int | None


@dataclass(frozen=True)
class SavedAccount:
    label: str
    account_id: str
    email: str
    plan_type: str
    snapshot_name: str
    saved_at: str
    token_expires_at: int | None


@dataclass(frozen=True)
class UsageSnapshot:
    observed_at: int
    source: str
    plan_type: str | None
    limit_reached: bool
    primary_used_percent: int | None
    secondary_used_percent: int | None
    primary_window_minutes: int | None
    secondary_window_minutes: int | None
    primary_reset_at: int | None
    secondary_reset_at: int | None
    credits_has_credits: bool | None
    credits_balance: str | None
    credits_unlimited: bool | None
    local_tokens_used: int | None = None
    local_thread_count: int | None = None
    local_share_percent: float | None = None


@dataclass(frozen=True)
class AccountUsage:
    quota_snapshot: UsageSnapshot | None = None
    local_history_snapshot: UsageSnapshot | None = None


@dataclass(frozen=True)
class ActiveStatus:
    metadata: AuthMetadata | None
    saved_account: SavedAccount | None
    usage: AccountUsage | None
    quota_refresh_error: str | None = None


def normalize_label(label: str) -> str:
    normalized = label.strip()
    if not normalized:
        raise SwitcherError("Label must not be empty.")
    return normalized


def label_key(label: str) -> str:
    return normalize_label(label).casefold()


def safe_label_fragment(label: str) -> str:
    fragment = SAFE_LABEL_PATTERN.sub("-", normalize_label(label)).strip(".-")
    return fragment or "account"


def now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json_file(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise SwitcherError(f"Codex auth file not found at {path}.") from exc
    except OSError as exc:
        raise SwitcherError(f"Failed to read {path}: {exc}.") from exc

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwitcherError(f"{path} is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise SwitcherError(f"{path} must contain a JSON object.")

    return payload, raw_bytes


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise SwitcherError("Codex access token is not a valid JWT.")

    padded = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwitcherError("Failed to decode the Codex access token payload.") from exc

    if not isinstance(payload, dict):
        raise SwitcherError("Codex access token payload must be a JSON object.")

    return payload


def extract_auth_metadata(auth_payload: dict[str, Any]) -> AuthMetadata:
    tokens = auth_payload.get("tokens")
    if not isinstance(tokens, dict):
        raise SwitcherError("Codex auth.json does not contain OAuth tokens.")

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise SwitcherError("Codex auth.json does not contain a ChatGPT OAuth access token.")

    jwt_payload = decode_jwt_payload(access_token)
    auth_claims = jwt_payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}

    profile_claims = jwt_payload.get("https://api.openai.com/profile")
    if not isinstance(profile_claims, dict):
        profile_claims = {}

    account_id = auth_claims.get("chatgpt_account_id") or tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise SwitcherError("Codex auth.json does not contain a ChatGPT account id.")

    email = profile_claims.get("email")
    if not isinstance(email, str) or not email.strip():
        email = "unknown"

    plan_type = auth_claims.get("chatgpt_plan_type")
    if not isinstance(plan_type, str) or not plan_type.strip():
        plan_type = "unknown"

    user_id = auth_claims.get("chatgpt_user_id") or auth_claims.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        user_id = None

    token_expires_at = jwt_payload.get("exp")
    if not isinstance(token_expires_at, int):
        token_expires_at = None

    token_issued_at = jwt_payload.get("iat")
    if not isinstance(token_issued_at, int):
        token_issued_at = None

    auth_mode = auth_payload.get("auth_mode")
    if not isinstance(auth_mode, str):
        auth_mode = None

    return AuthMetadata(
        account_id=account_id,
        email=email,
        plan_type=plan_type,
        user_id=user_id,
        auth_mode=auth_mode,
        token_expires_at=token_expires_at,
        token_issued_at=token_issued_at,
    )


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def normalize_chatgpt_base_url(base_url: str) -> str:
    normalized = base_url.strip()
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized:
        return DEFAULT_CHATGPT_BASE_URL
    if (
        (normalized.startswith("https://chatgpt.com") or normalized.startswith("https://chat.openai.com"))
        and "/backend-api" not in normalized
    ):
        normalized = f"{normalized}/backend-api"
    return normalized


def load_chatgpt_base_url(codex_home: Path) -> str:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return DEFAULT_CHATGPT_BASE_URL

    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SwitcherError(f"Failed to read Codex config at {config_path}: {exc}.") from exc

    base_url = payload.get("chatgpt_base_url")
    if isinstance(base_url, str) and base_url.strip():
        return normalize_chatgpt_base_url(base_url)
    return DEFAULT_CHATGPT_BASE_URL


def json_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers is not None:
        request_headers.update(headers)

    data = None
    method = "GET"
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        method = "POST"

    request = urllib.request.Request(url, headers=request_headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise LiveQuotaUnauthorizedError(response_body or f"HTTP 401 from {url}.") from exc
        detail = response_body or getattr(exc, "reason", "") or f"HTTP {exc.code}"
        raise SwitcherError(f"{method} {url} failed: {detail}.") from exc
    except urllib.error.URLError as exc:
        raise SwitcherError(f"{method} {url} failed: {exc.reason}.") from exc
    except OSError as exc:
        raise SwitcherError(f"{method} {url} failed: {exc}.") from exc

    if not body:
        return {}

    try:
        payload_value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwitcherError(f"{method} {url} returned invalid JSON.") from exc

    if not isinstance(payload_value, dict):
        raise SwitcherError(f"{method} {url} returned a non-object JSON payload.")
    return payload_value


def mapping_get(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def parse_window_minutes(window: dict[str, Any]) -> int | None:
    window_minutes = parse_int(mapping_get(window, "window_minutes", "windowMinutes", "window_duration_mins", "windowDurationMins"))
    if window_minutes is not None:
        return window_minutes
    limit_window_seconds = parse_int(mapping_get(window, "limit_window_seconds", "limitWindowSeconds"))
    if limit_window_seconds is None:
        return None
    return limit_window_seconds // 60


def parse_live_rate_limit_window(window: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(window, dict):
        return None, None, None

    return (
        parse_int(mapping_get(window, "used_percent", "usedPercent")),
        parse_window_minutes(window),
        parse_int(mapping_get(window, "reset_at", "resetAt", "resets_at", "resetsAt")),
    )


def usage_snapshot_from_live_payload(observed_at: int, payload: dict[str, Any]) -> UsageSnapshot | None:
    rate_limit = mapping_get(payload, "rate_limit", "rateLimit")
    if not isinstance(rate_limit, dict):
        return None

    primary_used_percent, primary_window_minutes, primary_reset_at = parse_live_rate_limit_window(
        mapping_get(rate_limit, "primary_window", "primaryWindow")
    )
    secondary_used_percent, secondary_window_minutes, secondary_reset_at = parse_live_rate_limit_window(
        mapping_get(rate_limit, "secondary_window", "secondaryWindow")
    )

    credits = payload.get("credits")
    if not isinstance(credits, dict):
        credits = {}

    plan_type = mapping_get(payload, "plan_type", "planType")
    if not isinstance(plan_type, str):
        plan_type = None

    return UsageSnapshot(
        observed_at=observed_at,
        source="live_rate_limits",
        plan_type=plan_type,
        limit_reached=bool(mapping_get(rate_limit, "limit_reached", "limitReached")),
        primary_used_percent=primary_used_percent,
        secondary_used_percent=secondary_used_percent,
        primary_window_minutes=primary_window_minutes,
        secondary_window_minutes=secondary_window_minutes,
        primary_reset_at=primary_reset_at,
        secondary_reset_at=secondary_reset_at,
        credits_has_credits=parse_bool(mapping_get(credits, "has_credits", "hasCredits")),
        credits_balance=str(credits.get("balance")) if credits.get("balance") is not None else None,
        credits_unlimited=parse_bool(credits.get("unlimited")),
    )


def token_needs_refresh(expires_at: int | None) -> bool:
    if expires_at is None:
        return False
    return expires_at <= int(time.time()) + 60


def refreshed_auth_payload(auth_payload: dict[str, Any]) -> dict[str, Any]:
    tokens = auth_payload.get("tokens")
    if not isinstance(tokens, dict):
        raise SwitcherError("Codex auth.json does not contain OAuth tokens.")

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise SwitcherError("Codex auth.json does not contain a ChatGPT refresh token.")

    refresh_url = os.environ.get(REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR, REFRESH_TOKEN_URL)
    try:
        refresh_response = json_request(
            refresh_url,
            payload={
                "client_id": REFRESH_TOKEN_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    except LiveQuotaUnauthorizedError as exc:
        detail = str(exc).strip() or "unauthorized"
        raise SwitcherError(f"Refresh token request was rejected: {detail}.") from exc

    access_token = refresh_response.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise SwitcherError("Refresh token request did not return an access token.")

    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = access_token
    refreshed_token = refresh_response.get("refresh_token")
    if isinstance(refreshed_token, str) and refreshed_token.strip():
        updated_tokens["refresh_token"] = refreshed_token
    refreshed_id_token = refresh_response.get("id_token")
    if isinstance(refreshed_id_token, str) and refreshed_id_token.strip():
        updated_tokens["id_token"] = refreshed_id_token

    updated_payload = dict(auth_payload)
    updated_payload["tokens"] = updated_tokens
    updated_payload["last_refresh"] = now_utc_iso()
    return updated_payload


def fetch_live_quota_snapshot(codex_home: Path, auth_payload: dict[str, Any]) -> tuple[UsageSnapshot, dict[str, Any]]:
    metadata = extract_auth_metadata(auth_payload)
    if metadata.auth_mode is not None and metadata.auth_mode.casefold() != "chatgpt":
        raise SwitcherError("Active auth is not using ChatGPT OAuth.")

    base_url = load_chatgpt_base_url(codex_home)
    usage_url = f"{base_url}/wham/usage" if "/backend-api" in base_url else f"{base_url}/api/codex/usage"
    current_payload = auth_payload
    current_metadata = metadata

    if token_needs_refresh(metadata.token_expires_at):
        current_payload = refreshed_auth_payload(current_payload)
        current_metadata = extract_auth_metadata(current_payload)

    request_headers = {
        "Authorization": f"Bearer {current_payload['tokens']['access_token']}",
        "ChatGPT-Account-Id": current_metadata.account_id,
    }
    try:
        live_payload = json_request(usage_url, headers=request_headers)
    except LiveQuotaUnauthorizedError:
        current_payload = refreshed_auth_payload(current_payload)
        current_metadata = extract_auth_metadata(current_payload)
        request_headers = {
            "Authorization": f"Bearer {current_payload['tokens']['access_token']}",
            "ChatGPT-Account-Id": current_metadata.account_id,
        }
        try:
            live_payload = json_request(usage_url, headers=request_headers)
        except LiveQuotaUnauthorizedError as exc:
            detail = str(exc).strip() or "unauthorized"
            raise SwitcherError(f"Live ChatGPT quota fetch remained unauthorized after refresh: {detail}.") from exc

    snapshot = usage_snapshot_from_live_payload(int(time.time()), live_payload)
    if snapshot is None:
        raise SwitcherError("Live ChatGPT quota response did not contain a usable rate limit snapshot.")

    refreshed_metadata = extract_auth_metadata(current_payload)
    if refreshed_metadata.account_id != metadata.account_id:
        raise SwitcherError("Refreshed auth resolved to a different account id.")
    return snapshot, current_payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=f"{path.stem}-",
        suffix=".tmp",
    )
    temp_path = Path(temp_handle.name)
    try:
        with temp_handle:
            temp_handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        os.replace(temp_path, path)
    except OSError as exc:
        raise SwitcherError(f"Failed to write {path}: {exc}.") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def detect_log_body_column(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA table_info(logs)").fetchall()
    column_names = {row[1] for row in rows if len(row) > 1}
    if "message" in column_names:
        return "message"
    if "feedback_log_body" in column_names:
        return "feedback_log_body"
    raise sqlite3.DatabaseError("logs table is missing both message and feedback_log_body columns.")


def extract_account_id_from_log_message(message: str) -> str | None:
    match = ACCOUNT_ID_PATTERN.search(message)
    if match:
        return match.group(1).strip()

    match = OTEL_ACCOUNT_ID_PATTERN.search(message)
    if match:
        return match.group(1).strip()

    return None


def load_account_mappings(
    connection: sqlite3.Connection,
) -> tuple[str, dict[str, str], dict[str, list[tuple[int, str]]], dict[str, set[str]], dict[str, int]]:
    body_column = detect_log_body_column(connection)
    rows = connection.execute(
        f"""
        SELECT id, ts, thread_id, process_uuid, {body_column} AS body
        FROM logs
        WHERE (
            target = 'log'
            AND {body_column} LIKE ?
        ) OR (
            target IN ('codex_otel.log_only', 'codex_otel.trace_safe')
            AND thread_id IS NOT NULL
            AND {body_column} LIKE ?
        )
        ORDER BY id ASC
        """,
        (f"{REQUEST_MESSAGE_PREFIX}%", '%user.account_id="%"%'),
    ).fetchall()

    thread_to_account: dict[str, str] = {}
    process_to_requests: dict[str, list[tuple[int, str]]] = {}
    account_threads: dict[str, set[str]] = {}
    account_last_seen: dict[str, int] = {}
    for row in rows:
        message = row["body"]
        if not isinstance(message, str):
            continue

        account_id = extract_account_id_from_log_message(message)
        if not account_id:
            continue

        thread_id = row["thread_id"]
        if isinstance(thread_id, str) and thread_id:
            thread_to_account[thread_id] = account_id
            account_threads.setdefault(account_id, set()).add(thread_id)

        process_uuid = row["process_uuid"]
        if isinstance(process_uuid, str) and process_uuid:
            process_to_requests.setdefault(process_uuid, []).append((int(row["id"]), account_id))

        ts_value = parse_int(row["ts"])
        if ts_value is None:
            continue

        previous = account_last_seen.get(account_id)
        if previous is None or ts_value > previous:
            account_last_seen[account_id] = ts_value

    return body_column, thread_to_account, process_to_requests, account_threads, account_last_seen


def resolve_account_id(
    event_id: int,
    thread_id: Any,
    process_uuid: Any,
    thread_to_account: dict[str, str],
    process_to_requests: dict[str, list[tuple[int, str]]],
) -> str | None:
    if isinstance(thread_id, str) and thread_id in thread_to_account:
        return thread_to_account[thread_id]

    if not isinstance(process_uuid, str) or process_uuid not in process_to_requests:
        return None

    for request_id, account_id in reversed(process_to_requests[process_uuid]):
        if request_id <= event_id:
            return account_id
    return None


def usage_snapshot_from_payload(observed_at: int, payload: dict[str, Any]) -> UsageSnapshot | None:
    event_type = payload.get("type")
    if event_type == "codex.rate_limits":
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return None

        primary = rate_limits.get("primary")
        if not isinstance(primary, dict):
            primary = {}
        secondary = rate_limits.get("secondary")
        if not isinstance(secondary, dict):
            secondary = {}
        credits = payload.get("credits")
        if not isinstance(credits, dict):
            credits = {}

        plan_type = payload.get("plan_type")
        if not isinstance(plan_type, str):
            plan_type = None

        return UsageSnapshot(
            observed_at=observed_at,
            source="rate_limits",
            plan_type=plan_type,
            limit_reached=bool(rate_limits.get("limit_reached")),
            primary_used_percent=parse_int(primary.get("used_percent")),
            secondary_used_percent=parse_int(secondary.get("used_percent")),
            primary_window_minutes=parse_int(primary.get("window_minutes")),
            secondary_window_minutes=parse_int(secondary.get("window_minutes")),
            primary_reset_at=parse_int(primary.get("reset_at")),
            secondary_reset_at=parse_int(secondary.get("reset_at")),
            credits_has_credits=parse_bool(credits.get("has_credits")),
            credits_balance=str(credits.get("balance")) if credits.get("balance") is not None else None,
            credits_unlimited=parse_bool(credits.get("unlimited")),
        )

    if event_type == "error":
        error_payload = payload.get("error")
        if not isinstance(error_payload, dict) or error_payload.get("type") != "usage_limit_reached":
            return None

        headers = payload.get("headers")
        if not isinstance(headers, dict):
            headers = {}

        plan_type = error_payload.get("plan_type")
        if not isinstance(plan_type, str):
            header_plan_type = headers.get("X-Codex-Plan-Type")
            plan_type = header_plan_type if isinstance(header_plan_type, str) else None

        return UsageSnapshot(
            observed_at=observed_at,
            source="usage_limit_reached",
            plan_type=plan_type,
            limit_reached=True,
            primary_used_percent=parse_int(headers.get("X-Codex-Primary-Used-Percent")),
            secondary_used_percent=parse_int(headers.get("X-Codex-Secondary-Used-Percent")),
            primary_window_minutes=parse_int(headers.get("X-Codex-Primary-Window-Minutes")),
            secondary_window_minutes=parse_int(headers.get("X-Codex-Secondary-Window-Minutes")),
            primary_reset_at=parse_int(headers.get("X-Codex-Primary-Reset-At")) or parse_int(error_payload.get("resets_at")),
            secondary_reset_at=parse_int(headers.get("X-Codex-Secondary-Reset-At")),
            credits_has_credits=parse_bool(headers.get("X-Codex-Credits-Has-Credits")),
            credits_balance=(
                str(headers.get("X-Codex-Credits-Balance"))
                if headers.get("X-Codex-Credits-Balance") is not None
                else None
            ),
            credits_unlimited=parse_bool(headers.get("X-Codex-Credits-Unlimited")),
        )

    return None


def parse_usage_payload(message: str) -> dict[str, Any] | None:
    for prefix in (WS_EVENT_PREFIX, RECEIVED_MESSAGE_PREFIX):
        if not message.startswith(prefix):
            continue
        try:
            payload = json.loads(message[len(prefix):])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def load_latest_usage_by_account(logs_path: Path) -> dict[str, UsageSnapshot]:
    if not logs_path.exists():
        return {}

    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{logs_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        body_column, thread_to_account, process_to_requests, _, _ = load_account_mappings(connection)

        event_rows = connection.execute(
            f"""
            SELECT id, ts, thread_id, process_uuid, {body_column} AS body
            FROM logs
            WHERE (
                target = 'codex_api::endpoint::responses_websocket'
                AND (
                    {body_column} LIKE 'websocket event: {{"type":"codex.rate_limits"%'
                    OR {body_column} LIKE 'websocket event: {{"type":"error","error":{{"type":"usage_limit_reached"%'
                )
            ) OR (
                target = 'log'
                AND (
                    {body_column} LIKE 'Received message {{"type":"codex.rate_limits"%'
                    OR {body_column} LIKE 'Received message {{"type":"error","error":{{"type":"usage_limit_reached"%'
                )
            )
            ORDER BY id DESC
            """
        ).fetchall()

        latest_by_account: dict[str, UsageSnapshot] = {}
        for row in event_rows:
            message = row["body"]
            if not isinstance(message, str):
                continue

            account_id = resolve_account_id(
                event_id=int(row["id"]),
                thread_id=row["thread_id"],
                process_uuid=row["process_uuid"],
                thread_to_account=thread_to_account,
                process_to_requests=process_to_requests,
            )
            if not account_id or account_id in latest_by_account:
                continue

            payload = parse_usage_payload(message)
            if payload is None:
                continue

            snapshot = usage_snapshot_from_payload(int(row["ts"]), payload)
            if snapshot is not None:
                latest_by_account[account_id] = snapshot

        return latest_by_account
    except sqlite3.DatabaseError:
        return {}
    finally:
        if connection is not None:
            connection.close()


def load_local_usage_by_account(logs_path: Path, state_path: Path) -> dict[str, UsageSnapshot]:
    if not logs_path.exists() or not state_path.exists():
        return {}

    logs_connection: sqlite3.Connection | None = None
    state_connection: sqlite3.Connection | None = None
    try:
        logs_uri = f"file:{logs_path.as_posix()}?mode=ro"
        logs_connection = sqlite3.connect(logs_uri, uri=True)
        logs_connection.row_factory = sqlite3.Row
        _, _, _, account_threads, account_last_seen = load_account_mappings(logs_connection)

        if not account_threads:
            return {}

        state_uri = f"file:{state_path.as_posix()}?mode=ro"
        state_connection = sqlite3.connect(state_uri, uri=True)
        state_connection.row_factory = sqlite3.Row

        local_usage: dict[str, UsageSnapshot] = {}
        for account_id, thread_ids in account_threads.items():
            placeholders = ",".join("?" for _ in thread_ids)
            row = state_connection.execute(
                f"""
                SELECT
                    COUNT(*) AS thread_count,
                    COALESCE(SUM(tokens_used), 0) AS total_tokens,
                    MAX(updated_at) AS last_updated
                FROM threads
                WHERE id IN ({placeholders})
                """,
                list(thread_ids),
            ).fetchone()

            if row is None:
                continue

            thread_count = parse_int(row["thread_count"])
            total_tokens = parse_int(row["total_tokens"])
            observed_at = parse_int(row["last_updated"]) or account_last_seen.get(account_id)
            if not thread_count or observed_at is None:
                continue

            local_usage[account_id] = UsageSnapshot(
                observed_at=observed_at,
                source="local_threads",
                plan_type=None,
                limit_reached=False,
                primary_used_percent=None,
                secondary_used_percent=None,
                primary_window_minutes=None,
                secondary_window_minutes=None,
                primary_reset_at=None,
                secondary_reset_at=None,
                credits_has_credits=None,
                credits_balance=None,
                credits_unlimited=None,
                local_tokens_used=total_tokens,
                local_thread_count=thread_count,
            )

        total_local_tokens = sum(
            snapshot.local_tokens_used or 0
            for snapshot in local_usage.values()
        )
        if total_local_tokens <= 0:
            return local_usage

        return {
            account_id: replace(
                snapshot,
                local_share_percent=((snapshot.local_tokens_used or 0) * 100.0 / total_local_tokens),
            )
            for account_id, snapshot in local_usage.items()
        }
    except sqlite3.DatabaseError:
        return {}
    finally:
        if logs_connection is not None:
            logs_connection.close()
        if state_connection is not None:
            state_connection.close()


def load_recent_local_usage_since(state_path: Path, observed_after: int) -> UsageSnapshot | None:
    if not state_path.exists():
        return None

    state_connection: sqlite3.Connection | None = None
    try:
        state_uri = f"file:{state_path.as_posix()}?mode=ro"
        state_connection = sqlite3.connect(state_uri, uri=True)
        state_connection.row_factory = sqlite3.Row
        row = state_connection.execute(
            """
            SELECT
                COUNT(*) AS thread_count,
                COALESCE(SUM(tokens_used), 0) AS total_tokens,
                MAX(updated_at) AS last_updated
            FROM threads
            WHERE updated_at >= ?
            """,
            (observed_after,),
        ).fetchone()
        if row is None:
            return None

        thread_count = parse_int(row["thread_count"])
        total_tokens = parse_int(row["total_tokens"])
        observed_at = parse_int(row["last_updated"])
        if not thread_count or observed_at is None:
            return None

        return UsageSnapshot(
            observed_at=observed_at,
            source="active_auth_local_threads",
            plan_type=None,
            limit_reached=False,
            primary_used_percent=None,
            secondary_used_percent=None,
            primary_window_minutes=None,
            secondary_window_minutes=None,
            primary_reset_at=None,
            secondary_reset_at=None,
            credits_has_credits=None,
            credits_balance=None,
            credits_unlimited=None,
            local_tokens_used=total_tokens,
            local_thread_count=thread_count,
        )
    except sqlite3.DatabaseError:
        return None
    finally:
        if state_connection is not None:
            state_connection.close()


def load_usage_by_account(logs_path: Path, state_path: Path | None = None) -> dict[str, AccountUsage]:
    usage = {
        account_id: AccountUsage(quota_snapshot=snapshot)
        for account_id, snapshot in load_latest_usage_by_account(logs_path).items()
    }
    if state_path is None:
        return usage

    for account_id, local_snapshot in load_local_usage_by_account(logs_path, state_path).items():
        existing = usage.get(account_id)
        if existing is None:
            usage[account_id] = AccountUsage(local_history_snapshot=local_snapshot)
            continue
        usage[account_id] = replace(existing, local_history_snapshot=local_snapshot)

    return usage


def format_timestamp(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def format_token_count(tokens: int | None) -> str:
    if tokens is None:
        return "?"

    magnitude = abs(tokens)
    if magnitude >= 1_000_000_000:
        return f"{tokens / 1_000_000_000:.1f}B"
    if magnitude >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return f"{tokens:,}"


def format_window_label(window_minutes: int | None, fallback: str) -> str:
    if window_minutes is None:
        return fallback

    minutes_per_hour = 60
    minutes_per_day = 24 * minutes_per_hour
    minutes_per_week = 7 * minutes_per_day
    minutes_per_month = 30 * minutes_per_day
    rounding_bias_minutes = 3
    adjusted_minutes = max(0, window_minutes)

    if adjusted_minutes <= minutes_per_day + rounding_bias_minutes:
        hours = max(1, (adjusted_minutes + rounding_bias_minutes) // minutes_per_hour)
        return f"{hours}h"
    if adjusted_minutes <= minutes_per_week + rounding_bias_minutes:
        return "weekly"
    if adjusted_minutes <= minutes_per_month + rounding_bias_minutes:
        return "monthly"
    return "annual"


def format_remaining_quota(used_percent: int | None) -> str:
    if used_percent is None:
        return "? left"
    return f"{max(0, 100 - used_percent)}% left"


def format_thread_count(thread_count: int | None) -> str:
    if thread_count is None:
        return "? threads"
    if thread_count == 1:
        return "1 thread"
    return f"{thread_count} threads"


def format_credit_balance(balance: str | None) -> str | None:
    if balance is None:
        return None

    trimmed = balance.strip()
    if not trimmed:
        return None

    try:
        int_value = int(trimmed)
    except ValueError:
        try:
            float_value = float(trimmed)
        except ValueError:
            return None
        if float_value <= 0:
            return None
        return str(round(float_value))

    if int_value <= 0:
        return None
    return str(int_value)


def summarize_quota_snapshot(snapshot: UsageSnapshot) -> str:
    parts: list[str] = []

    if snapshot.primary_used_percent is not None or snapshot.primary_window_minutes is not None:
        parts.append(
            f"{format_window_label(snapshot.primary_window_minutes, '5h')} "
            f"{format_remaining_quota(snapshot.primary_used_percent)}"
        )
    if snapshot.secondary_used_percent is not None or snapshot.secondary_window_minutes is not None:
        parts.append(
            f"{format_window_label(snapshot.secondary_window_minutes, 'weekly')} "
            f"{format_remaining_quota(snapshot.secondary_used_percent)}"
        )

    if snapshot.credits_unlimited:
        parts.append("credits unlimited")
    elif snapshot.credits_has_credits:
        credit_balance = format_credit_balance(snapshot.credits_balance)
        if credit_balance is not None:
            parts.append(f"credits {credit_balance}")

    if snapshot.limit_reached and not parts:
        parts.append("limit reached")

    return "; ".join(parts) if parts else "quota snapshot"


def summarize_local_history_snapshot(snapshot: UsageSnapshot) -> str:
    token_text = format_token_count(snapshot.local_tokens_used)
    thread_text = format_thread_count(snapshot.local_thread_count)
    if snapshot.source == "active_auth_local_threads":
        return f"local history since active auth {token_text} ({thread_text})"

    share_text = (
        f"{snapshot.local_share_percent:.1f}%"
        if snapshot.local_share_percent is not None
        else "?"
    )
    return f"local history {token_text} ({share_text}, {thread_text})"


def summarize_usage(usage: AccountUsage | None) -> str:
    if usage is None:
        return "unknown"

    parts: list[str] = []
    if usage.quota_snapshot is not None:
        parts.append(summarize_quota_snapshot(usage.quota_snapshot))
    if usage.local_history_snapshot is not None:
        parts.append(summarize_local_history_snapshot(usage.local_history_snapshot))
    return "; ".join(parts) if parts else "unknown"


def short_account_id(account_id: str) -> str:
    return account_id[:8]


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = []
    lines.append("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


class SwitcherService:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def add_current_account(self, label: str) -> SavedAccount:
        label = normalize_label(label)
        auth_payload, raw_bytes = read_json_file(self.paths.auth_path)
        metadata = extract_auth_metadata(auth_payload)

        registry = self._load_registry()
        normalized_key = label_key(label)

        existing_label = registry.get(normalized_key)
        if existing_label and existing_label.account_id != metadata.account_id:
            raise SwitcherError(
                f"Label '{label}' already exists for a different account ({existing_label.email}, {existing_label.account_id})."
            )

        for existing_account in registry.values():
            if existing_account.account_id == metadata.account_id and label_key(existing_account.label) != normalized_key:
                raise SwitcherError(
                    f"Account {metadata.account_id} is already saved as '{existing_account.label}'."
                )

        self.paths.switcher_home.mkdir(parents=True, exist_ok=True)
        self.paths.accounts_dir.mkdir(parents=True, exist_ok=True)

        snapshot_name = (
            existing_label.snapshot_name
            if existing_label is not None
            else f"{safe_label_fragment(label)}--{metadata.account_id}.auth.json"
        )
        (self.paths.accounts_dir / snapshot_name).write_bytes(raw_bytes)

        saved_account = SavedAccount(
            label=label,
            account_id=metadata.account_id,
            email=metadata.email,
            plan_type=metadata.plan_type,
            snapshot_name=snapshot_name,
            saved_at=now_utc_iso(),
            token_expires_at=metadata.token_expires_at,
        )
        registry[normalized_key] = saved_account
        self._save_registry(registry)
        return saved_account

    def list_accounts(self) -> list[SavedAccount]:
        return sorted(self._load_registry().values(), key=lambda account: account.label.casefold())

    def switch_account(self, label: str) -> SavedAccount:
        label = normalize_label(label)
        registry = self._load_registry()
        saved_account = registry.get(label_key(label))
        if saved_account is None:
            raise SwitcherError(f"No saved account found for label '{label}'.")

        snapshot_path = self.paths.accounts_dir / saved_account.snapshot_name
        snapshot_payload, raw_bytes = read_json_file(snapshot_path)
        extract_auth_metadata(snapshot_payload)

        self.paths.codex_home.mkdir(parents=True, exist_ok=True)
        temp_handle = tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=self.paths.codex_home,
            prefix="auth-",
            suffix=".tmp",
        )
        temp_path = Path(temp_handle.name)
        try:
            with temp_handle:
                temp_handle.write(raw_bytes)
            os.replace(temp_path, self.paths.auth_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

        return saved_account

    def try_read_current_auth(self) -> AuthMetadata | None:
        if not self.paths.auth_path.exists():
            return None
        auth_payload, _ = read_json_file(self.paths.auth_path)
        return extract_auth_metadata(auth_payload)

    def active_auth_observed_at(self) -> int | None:
        if not self.paths.auth_path.exists():
            return None
        try:
            return int(self.paths.auth_path.stat().st_mtime)
        except OSError:
            return None

    def augment_usage_with_active_history(
        self,
        metadata: AuthMetadata | None,
        usage: AccountUsage | None,
    ) -> AccountUsage | None:
        if metadata is None:
            return usage
        if usage is not None and usage.local_history_snapshot is not None:
            return usage

        observed_after = self.active_auth_observed_at()
        if observed_after is None:
            return usage

        fallback_snapshot = load_recent_local_usage_since(self.paths.state_path, observed_after)
        if fallback_snapshot is None:
            return usage
        if usage is None:
            return AccountUsage(local_history_snapshot=fallback_snapshot)
        return replace(usage, local_history_snapshot=fallback_snapshot)

    def get_active_status(self, refresh_quota: bool = False) -> ActiveStatus:
        if not self.paths.auth_path.exists():
            return ActiveStatus(metadata=None, saved_account=None, usage=None)

        auth_payload, _ = read_json_file(self.paths.auth_path)
        metadata = extract_auth_metadata(auth_payload)
        saved_account = None
        for account in self._load_registry().values():
            if account.account_id == metadata.account_id:
                saved_account = account
                break

        usage = self.load_usage().get(metadata.account_id)
        quota_refresh_error = None
        if refresh_quota:
            try:
                live_snapshot, refreshed_payload = fetch_live_quota_snapshot(self.paths.codex_home, auth_payload)
                saved_account = self._persist_active_auth_payload(refreshed_payload, saved_account)
                metadata = extract_auth_metadata(refreshed_payload)
                if usage is None:
                    usage = AccountUsage(quota_snapshot=live_snapshot)
                else:
                    usage = replace(usage, quota_snapshot=live_snapshot)
            except SwitcherError as exc:
                quota_refresh_error = str(exc)

        usage = self.augment_usage_with_active_history(metadata, usage)
        return ActiveStatus(
            metadata=metadata,
            saved_account=saved_account,
            usage=usage,
            quota_refresh_error=quota_refresh_error,
        )

    def load_usage(self) -> dict[str, AccountUsage]:
        return load_usage_by_account(self.paths.logs_path, self.paths.state_path)

    def get_active_status_with_refresh(self) -> ActiveStatus:
        return self.get_active_status(refresh_quota=True)

    def _persist_active_auth_payload(
        self,
        auth_payload: dict[str, Any],
        saved_account: SavedAccount | None,
    ) -> SavedAccount | None:
        write_json_atomic(self.paths.auth_path, auth_payload)
        metadata = extract_auth_metadata(auth_payload)

        if saved_account is None:
            return None

        snapshot_path = self.paths.accounts_dir / saved_account.snapshot_name
        write_json_atomic(snapshot_path, auth_payload)

        updated_saved_account = replace(
            saved_account,
            email=metadata.email,
            plan_type=metadata.plan_type,
            token_expires_at=metadata.token_expires_at,
        )
        registry = self._load_registry()
        registry[label_key(saved_account.label)] = updated_saved_account
        self._save_registry(registry)
        return updated_saved_account

    def _load_registry(self) -> dict[str, SavedAccount]:
        if not self.paths.registry_path.exists():
            return {}

        payload, _ = read_json_file(self.paths.registry_path)
        version = payload.get("version")
        if version != REGISTRY_VERSION:
            raise SwitcherError(f"Unsupported registry version {version!r} in {self.paths.registry_path}.")

        account_entries = payload.get("accounts")
        if not isinstance(account_entries, list):
            raise SwitcherError(f"{self.paths.registry_path} must contain an 'accounts' list.")

        registry: dict[str, SavedAccount] = {}
        seen_account_ids: set[str] = set()
        for entry in account_entries:
            if not isinstance(entry, dict):
                raise SwitcherError(f"{self.paths.registry_path} contains an invalid account entry.")

            saved_account = SavedAccount(
                label=normalize_label(str(entry.get("label", ""))),
                account_id=str(entry.get("account_id", "")).strip(),
                email=str(entry.get("email", "")).strip() or "unknown",
                plan_type=str(entry.get("plan_type", "")).strip() or "unknown",
                snapshot_name=str(entry.get("snapshot_name", "")).strip(),
                saved_at=str(entry.get("saved_at", "")).strip(),
                token_expires_at=parse_int(entry.get("token_expires_at")),
            )
            if not saved_account.account_id or not saved_account.snapshot_name:
                raise SwitcherError(f"{self.paths.registry_path} contains an incomplete account entry.")

            key = label_key(saved_account.label)
            if key in registry:
                raise SwitcherError(f"{self.paths.registry_path} contains duplicate labels.")
            if saved_account.account_id in seen_account_ids:
                raise SwitcherError(f"{self.paths.registry_path} contains duplicate account ids.")

            registry[key] = saved_account
            seen_account_ids.add(saved_account.account_id)

        return registry

    def _save_registry(self, registry: dict[str, SavedAccount]) -> None:
        self.paths.switcher_home.mkdir(parents=True, exist_ok=True)
        serialized = {
            "version": REGISTRY_VERSION,
            "accounts": [asdict(account) for account in sorted(registry.values(), key=lambda item: item.label.casefold())],
        }

        temp_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=self.paths.switcher_home,
            prefix="registry-",
            suffix=".tmp",
        )
        temp_path = Path(temp_handle.name)
        try:
            with temp_handle:
                json.dump(serialized, temp_handle, indent=2)
                temp_handle.write("\n")
            os.replace(temp_path, self.paths.registry_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
