from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1
REQUEST_MESSAGE_PREFIX = 'Request: "GET /backend-api/codex/responses HTTP/1.1'
WS_EVENT_PREFIX = "websocket event: "

ACCOUNT_ID_PATTERN = re.compile(r"chatgpt-account-id:\s*(.+?)(?:\\r\\n|[\r\n]|$)")
SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class SwitcherError(RuntimeError):
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


@dataclass(frozen=True)
class ActiveStatus:
    metadata: AuthMetadata | None
    saved_account: SavedAccount | None
    usage: UsageSnapshot | None


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


def load_latest_usage_by_account(logs_path: Path) -> dict[str, UsageSnapshot]:
    if not logs_path.exists():
        return {}

    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{logs_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row

        request_rows = connection.execute(
            """
            SELECT id, thread_id, process_uuid, message
            FROM logs
            WHERE target = 'log'
              AND message LIKE ?
            ORDER BY id ASC
            """,
            (f"{REQUEST_MESSAGE_PREFIX}%",),
        ).fetchall()

        thread_to_account: dict[str, str] = {}
        process_to_requests: dict[str, list[tuple[int, str]]] = {}
        for row in request_rows:
            message = row["message"]
            if not isinstance(message, str):
                continue

            match = ACCOUNT_ID_PATTERN.search(message)
            if not match:
                continue

            account_id = match.group(1)
            thread_id = row["thread_id"]
            if isinstance(thread_id, str) and thread_id:
                thread_to_account[thread_id] = account_id

            process_uuid = row["process_uuid"]
            if isinstance(process_uuid, str) and process_uuid:
                process_to_requests.setdefault(process_uuid, []).append((int(row["id"]), account_id))

        event_rows = connection.execute(
            """
            SELECT id, ts, thread_id, process_uuid, message
            FROM logs
            WHERE target = 'codex_api::endpoint::responses_websocket'
              AND (
                message LIKE 'websocket event: {"type":"codex.rate_limits"%'
                OR message LIKE 'websocket event: {"type":"error","error":{"type":"usage_limit_reached"%'
              )
            ORDER BY id DESC
            """
        ).fetchall()

        latest_by_account: dict[str, UsageSnapshot] = {}
        for row in event_rows:
            message = row["message"]
            if not isinstance(message, str) or not message.startswith(WS_EVENT_PREFIX):
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

            try:
                payload = json.loads(message[len(WS_EVENT_PREFIX):])
            except json.JSONDecodeError:
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


def format_timestamp(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


def summarize_usage(snapshot: UsageSnapshot | None) -> str:
    if snapshot is None:
        return "unknown"

    status = "reached" if snapshot.limit_reached else "available"
    primary = f"{snapshot.primary_used_percent}%" if snapshot.primary_used_percent is not None else "?"
    secondary = f"{snapshot.secondary_used_percent}%" if snapshot.secondary_used_percent is not None else "?"
    reset_at = snapshot.primary_reset_at or snapshot.secondary_reset_at
    return f"{status} p:{primary} s:{secondary} reset:{format_timestamp(reset_at)}"


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

    def get_active_status(self) -> ActiveStatus:
        metadata = self.try_read_current_auth()
        if metadata is None:
            return ActiveStatus(metadata=None, saved_account=None, usage=None)

        saved_account = None
        for account in self._load_registry().values():
            if account.account_id == metadata.account_id:
                saved_account = account
                break

        usage = self.load_usage().get(metadata.account_id)
        return ActiveStatus(metadata=metadata, saved_account=saved_account, usage=usage)

    def load_usage(self) -> dict[str, UsageSnapshot]:
        return load_latest_usage_by_account(self.paths.logs_path)

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
