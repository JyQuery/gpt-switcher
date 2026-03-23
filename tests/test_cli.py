from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gpt_switcher.cli import main
from gpt_switcher.core import (
    AppPaths,
    LiveQuotaUnauthorizedError,
    SwitcherError,
    SwitcherService,
    extract_auth_metadata,
    load_latest_usage_by_account,
    load_usage_by_account,
    read_json_file,
    summarize_usage,
)


def jwt_segment(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    return encoded.decode("ascii")


def build_access_token(account_id: str, email: str, plan_type: str = "plus") -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan_type,
            "chatgpt_user_id": f"user-{account_id}",
        },
        "https://api.openai.com/profile": {
            "email": email,
            "email_verified": True,
        },
        "exp": 2_000_000_000,
        "iat": 1_999_990_000,
    }
    return f"{jwt_segment(header)}.{jwt_segment(payload)}."


def write_auth_file(path: Path, account_id: str, email: str, plan_type: str = "plus") -> None:
    payload = {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-03-20T00:00:00Z",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id-token",
            "access_token": build_access_token(account_id, email, plan_type),
            "refresh_token": "refresh-token",
            "account_id": account_id,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def create_logs_db(path: Path, body_column: str = "message") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        f"""
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            ts_nanos INTEGER NOT NULL DEFAULT 0,
            level TEXT NOT NULL DEFAULT 'INFO',
            target TEXT NOT NULL,
            {body_column} TEXT,
            module_path TEXT,
            file TEXT,
            line INTEGER,
            thread_id TEXT,
            process_uuid TEXT,
            estimated_bytes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.commit()
    connection.close()


def create_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'cli',
            model_provider TEXT NOT NULL DEFAULT 'openai',
            cwd TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            sandbox_policy TEXT NOT NULL DEFAULT '',
            approval_mode TEXT NOT NULL DEFAULT '',
            tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            git_sha TEXT,
            git_branch TEXT,
            git_origin_url TEXT,
            cli_version TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '',
            agent_nickname TEXT,
            agent_role TEXT,
            memory_mode TEXT NOT NULL DEFAULT 'enabled',
            model TEXT,
            reasoning_effort TEXT
        )
        """
    )
    connection.commit()
    connection.close()


def insert_thread(path: Path, *, thread_id: str, tokens_used: int, updated_at: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO threads (id, updated_at, tokens_used, rollout_path, source, model_provider, cwd, title, sandbox_policy, approval_mode, cli_version, first_user_message, memory_mode)
        VALUES (?, ?, ?, '', 'cli', 'openai', '', '', '', '', '', '', 'enabled')
        """,
        (thread_id, updated_at, tokens_used),
    )
    connection.commit()
    connection.close()


def insert_log(
    path: Path,
    *,
    ts: int,
    target: str,
    message: str,
    thread_id: str,
    process_uuid: str = "proc-1",
    body_column: str = "message",
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        f"""
        INSERT INTO logs (ts, target, {body_column}, thread_id, process_uuid)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, target, message, thread_id, process_uuid),
    )
    connection.commit()
    connection.close()


def request_message(account_id: str) -> str:
    return (
        'Request: "GET /backend-api/codex/responses HTTP/1.1\\r\\n'
        f"chatgpt-account-id: {account_id}\\r\\n"
        "authorization: Bearer token\\r\\n"
        '\\r\\n"'
    )


def otel_account_message(account_id: str, email: str = "user@example.com") -> str:
    return (
        'event.name="codex.user_prompt" auth_mode="Chatgpt" '
        f'user.account_id="{account_id}" user.email="{email}"'
    )


def websocket_event(payload: dict) -> str:
    return "websocket event: " + json.dumps(payload, separators=(",", ":"))


def received_message(payload: dict) -> str:
    return "Received message " + json.dumps(payload, separators=(",", ":"))


class SwitcherCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.codex_home = root / "codex"
        self.switcher_home = root / "switcher"
        self.paths = AppPaths(codex_home=self.codex_home, switcher_home=self.switcher_home)
        self.service = SwitcherService(self.paths)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.codex_home),
                    "GPT_SWITCHER_HOME": str(self.switcher_home),
                },
                clear=False,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                exit_code = main(list(argv))
            except SystemExit as exc:
                exit_code = int(exc.code)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_add_saves_current_account_snapshot(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")

        saved = self.service.add_current_account("personal")

        self.assertEqual(saved.label, "personal")
        self.assertEqual(saved.email, "one@example.com")
        self.assertTrue((self.paths.accounts_dir / saved.snapshot_name).exists())

        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["accounts"][0]["account_id"], "account-1")

    def test_add_requires_existing_auth_file(self) -> None:
        with self.assertRaises(SwitcherError):
            self.service.add_current_account("missing")

    def test_add_rejects_duplicate_account_under_different_label(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")

        with self.assertRaises(SwitcherError):
            self.service.add_current_account("work")

    def test_switch_replaces_active_auth_file(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("one")

        write_auth_file(self.paths.auth_path, "account-2", "two@example.com")
        self.service.add_current_account("two")

        self.service.switch_account("one")

        payload, _ = read_json_file(self.paths.auth_path)
        metadata = extract_auth_metadata(payload)
        self.assertEqual(metadata.account_id, "account-1")
        self.assertEqual(metadata.email, "one@example.com")

    def test_status_command_is_not_available(self) -> None:
        exit_code, stdout, stderr = self.run_cli("status")

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("invalid choice: 'status'", stderr)

    def test_list_prefers_current_auth_plan_for_active_account(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com", plan_type="free")
        self.service.add_current_account("personal")
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com", plan_type="plus")

        exit_code, stdout, stderr = self.run_cli("list")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("personal", stdout)
        self.assertIn("plus", stdout)
        self.assertNotIn("free", stdout)

    def test_usage_parser_handles_rate_limits_event(self) -> None:
        create_logs_db(self.paths.logs_path)
        insert_log(
            self.paths.logs_path,
            ts=100,
            target="log",
            message=request_message("account-1"),
            thread_id="thread-1",
        )
        insert_log(
            self.paths.logs_path,
            ts=101,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary": {
                            "used_percent": 45,
                            "window_minutes": 300,
                            "reset_at": 1773969961,
                        },
                        "secondary": {
                            "used_percent": 14,
                            "window_minutes": 10080,
                            "reset_at": 1774471692,
                        },
                    },
                    "credits": None,
                }
            ),
            thread_id="thread-1",
        )

        usage = load_latest_usage_by_account(self.paths.logs_path)

        snapshot = usage["account-1"]
        self.assertFalse(snapshot.limit_reached)
        self.assertEqual(snapshot.primary_used_percent, 45)
        self.assertEqual(snapshot.secondary_used_percent, 14)
        self.assertEqual(snapshot.primary_reset_at, 1773969961)

    def test_usage_parser_handles_usage_limit_event(self) -> None:
        create_logs_db(self.paths.logs_path)
        insert_log(
            self.paths.logs_path,
            ts=200,
            target="log",
            message=request_message("account-2"),
            thread_id="thread-2",
        )
        insert_log(
            self.paths.logs_path,
            ts=201,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "error",
                    "error": {
                        "type": "usage_limit_reached",
                        "message": "The usage limit has been reached",
                        "plan_type": "plus",
                        "resets_at": 1773969961,
                    },
                    "status_code": 429,
                    "headers": {
                        "X-Codex-Plan-Type": "plus",
                        "X-Codex-Primary-Used-Percent": "100",
                        "X-Codex-Secondary-Used-Percent": "27",
                        "X-Codex-Primary-Window-Minutes": "300",
                        "X-Codex-Secondary-Window-Minutes": "10080",
                        "X-Codex-Primary-Reset-At": "1773969962",
                        "X-Codex-Secondary-Reset-At": "1774471692",
                        "X-Codex-Credits-Has-Credits": "False",
                        "X-Codex-Credits-Balance": "0",
                        "X-Codex-Credits-Unlimited": "False",
                    },
                }
            ),
            thread_id="thread-2",
        )

        usage = load_latest_usage_by_account(self.paths.logs_path)

        snapshot = usage["account-2"]
        self.assertTrue(snapshot.limit_reached)
        self.assertEqual(snapshot.primary_used_percent, 100)
        self.assertEqual(snapshot.secondary_used_percent, 27)
        self.assertEqual(snapshot.primary_reset_at, 1773969962)
        self.assertEqual(snapshot.credits_balance, "0")

    def test_usage_parser_prefers_newer_usage_limit_log_message(self) -> None:
        create_logs_db(self.paths.logs_path)
        insert_log(
            self.paths.logs_path,
            ts=200,
            target="log",
            message=request_message("account-2"),
            thread_id="thread-2",
            process_uuid="proc-2",
        )
        insert_log(
            self.paths.logs_path,
            ts=201,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 2,
                            "window_minutes": 300,
                            "reset_at": 1773969965,
                        },
                        "secondary": {
                            "used_percent": 54,
                            "window_minutes": 10080,
                            "reset_at": 1774471692,
                        },
                    },
                }
            ),
            thread_id="thread-2",
            process_uuid="proc-2",
        )
        insert_log(
            self.paths.logs_path,
            ts=300,
            target="log",
            message=request_message("account-2"),
            thread_id="thread-3",
            process_uuid="proc-3",
        )
        insert_log(
            self.paths.logs_path,
            ts=301,
            target="log",
            message=received_message(
                {
                    "type": "error",
                    "error": {
                        "type": "usage_limit_reached",
                        "message": "The usage limit has been reached",
                        "plan_type": "plus",
                        "resets_at": 1774471692,
                    },
                    "status_code": 429,
                    "headers": {
                        "X-Codex-Plan-Type": "plus",
                        "X-Codex-Primary-Used-Percent": "68",
                        "X-Codex-Secondary-Used-Percent": "100",
                        "X-Codex-Primary-Window-Minutes": "300",
                        "X-Codex-Secondary-Window-Minutes": "10080",
                        "X-Codex-Primary-Reset-At": "1774270091",
                        "X-Codex-Secondary-Reset-At": "1774471692",
                        "X-Codex-Credits-Has-Credits": "False",
                        "X-Codex-Credits-Balance": "0",
                        "X-Codex-Credits-Unlimited": "False",
                    },
                }
            ),
            thread_id="",
            process_uuid="proc-3",
        )

        usage = load_latest_usage_by_account(self.paths.logs_path)

        snapshot = usage["account-2"]
        self.assertTrue(snapshot.limit_reached)
        self.assertEqual(snapshot.primary_used_percent, 68)
        self.assertEqual(snapshot.secondary_used_percent, 100)
        self.assertEqual(snapshot.secondary_reset_at, 1774471692)
        summary = summarize_usage(load_usage_by_account(self.paths.logs_path)["account-2"])
        self.assertIn("weekly 0% left", summary)

    def test_usage_parser_maps_rate_limits_from_otel_account_rows(self) -> None:
        create_logs_db(self.paths.logs_path)
        insert_log(
            self.paths.logs_path,
            ts=100,
            target="codex_otel.log_only",
            message=otel_account_message("account-otel"),
            thread_id="thread-otel",
        )
        insert_log(
            self.paths.logs_path,
            ts=110,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 21,
                            "window_minutes": 300,
                            "reset_at": 1773969965,
                        },
                        "secondary": {
                            "used_percent": 55,
                            "window_minutes": 10080,
                            "reset_at": 1774569999,
                        },
                    },
                    "credits": {"has_credits": True, "balance": "12"},
                }
            ),
            thread_id="thread-otel",
        )

        usage = load_latest_usage_by_account(self.paths.logs_path)

        snapshot = usage["account-otel"]
        self.assertEqual(snapshot.source, "rate_limits")
        self.assertEqual(snapshot.primary_used_percent, 21)
        self.assertEqual(snapshot.secondary_used_percent, 55)

        summary = summarize_usage(load_usage_by_account(self.paths.logs_path)["account-otel"])
        self.assertIn("5h 79% left", summary)
        self.assertIn("weekly 45% left", summary)
        self.assertIn("credits 12", summary)

    def test_list_shows_unknown_usage_when_no_logs_exist(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")

        exit_code, stdout, stderr = self.run_cli("list")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("personal", stdout)
        self.assertIn("unknown", stdout)

    def test_list_shows_active_local_history_when_logs_cannot_map_account(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")
        create_state_db(self.paths.state_path)
        cutoff = 1_700_000_000
        os.utime(self.paths.auth_path, (cutoff, cutoff))
        insert_thread(self.paths.state_path, thread_id="thread-after", tokens_used=1200, updated_at=cutoff + 100)
        insert_thread(self.paths.state_path, thread_id="thread-before", tokens_used=3000, updated_at=cutoff - 100)

        exit_code, stdout, stderr = self.run_cli("list")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("local history since active auth 1.2K", stdout)
        self.assertNotIn("unknown", stdout)

    def test_usage_falls_back_to_local_thread_tokens(self) -> None:
        create_logs_db(self.paths.logs_path)
        create_state_db(self.paths.state_path)
        insert_log(
            self.paths.logs_path,
            ts=300,
            target="log",
            message=request_message("account-local"),
            thread_id="thread-local-1",
        )
        insert_log(
            self.paths.logs_path,
            ts=301,
            target="log",
            message=request_message("account-local"),
            thread_id="thread-local-2",
        )
        insert_thread(self.paths.state_path, thread_id="thread-local-1", tokens_used=1000, updated_at=555)
        insert_thread(self.paths.state_path, thread_id="thread-local-2", tokens_used=2500, updated_at=777)

        usage = load_usage_by_account(self.paths.logs_path, self.paths.state_path)

        self.assertIsNone(usage["account-local"].quota_snapshot)
        snapshot = usage["account-local"].local_history_snapshot
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source, "local_threads")
        self.assertEqual(snapshot.local_tokens_used, 3500)
        self.assertEqual(snapshot.local_thread_count, 2)
        self.assertEqual(snapshot.observed_at, 777)

    def test_usage_falls_back_to_local_thread_tokens_from_otel_account_rows(self) -> None:
        create_logs_db(self.paths.logs_path)
        create_state_db(self.paths.state_path)
        insert_log(
            self.paths.logs_path,
            ts=300,
            target="codex_otel.log_only",
            message=otel_account_message("account-local"),
            thread_id="thread-local-1",
        )
        insert_log(
            self.paths.logs_path,
            ts=301,
            target="codex_otel.log_only",
            message=otel_account_message("account-local"),
            thread_id="thread-local-2",
        )
        insert_thread(self.paths.state_path, thread_id="thread-local-1", tokens_used=1000, updated_at=555)
        insert_thread(self.paths.state_path, thread_id="thread-local-2", tokens_used=2500, updated_at=777)

        usage = load_usage_by_account(self.paths.logs_path, self.paths.state_path)

        self.assertIsNone(usage["account-local"].quota_snapshot)
        snapshot = usage["account-local"].local_history_snapshot
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source, "local_threads")
        self.assertEqual(snapshot.local_tokens_used, 3500)
        self.assertEqual(snapshot.local_thread_count, 2)
        self.assertEqual(snapshot.observed_at, 777)

    def test_usage_retains_last_known_quota_and_local_history_snapshots(self) -> None:
        create_logs_db(self.paths.logs_path)
        create_state_db(self.paths.state_path)
        insert_log(
            self.paths.logs_path,
            ts=100,
            target="codex_otel.log_only",
            message=otel_account_message("account-1"),
            thread_id="thread-remote",
        )
        insert_log(
            self.paths.logs_path,
            ts=110,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 5,
                            "window_minutes": 300,
                            "reset_at": 1773969965,
                        },
                        "secondary": {
                            "used_percent": 30,
                            "window_minutes": 10080,
                            "reset_at": 1774569999,
                        },
                    },
                }
            ),
            thread_id="thread-remote",
        )
        insert_log(
            self.paths.logs_path,
            ts=200,
            target="codex_otel.log_only",
            message=otel_account_message("account-1"),
            thread_id="thread-local",
        )
        insert_thread(self.paths.state_path, thread_id="thread-local", tokens_used=4200, updated_at=300)

        usage = load_usage_by_account(self.paths.logs_path, self.paths.state_path)

        quota_snapshot = usage["account-1"].quota_snapshot
        local_snapshot = usage["account-1"].local_history_snapshot
        self.assertIsNotNone(quota_snapshot)
        self.assertIsNotNone(local_snapshot)
        assert quota_snapshot is not None
        assert local_snapshot is not None
        self.assertEqual(quota_snapshot.source, "rate_limits")
        self.assertEqual(quota_snapshot.primary_used_percent, 5)
        self.assertEqual(quota_snapshot.observed_at, 110)
        self.assertEqual(local_snapshot.source, "local_threads")
        self.assertEqual(local_snapshot.local_tokens_used, 4200)
        self.assertEqual(local_snapshot.observed_at, 300)

    def test_list_shows_last_known_quota_and_local_history(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")
        create_logs_db(self.paths.logs_path)
        create_state_db(self.paths.state_path)
        insert_log(
            self.paths.logs_path,
            ts=100,
            target="log",
            message=request_message("account-1"),
            thread_id="thread-1",
        )
        insert_log(
            self.paths.logs_path,
            ts=110,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 5,
                            "window_minutes": 300,
                            "reset_at": 1773969965,
                        },
                        "secondary": {
                            "used_percent": 30,
                            "window_minutes": 10080,
                            "reset_at": 1774569999,
                        },
                    },
                }
            ),
            thread_id="thread-1",
        )
        insert_thread(self.paths.state_path, thread_id="thread-1", tokens_used=4200, updated_at=300)

        exit_code, stdout, stderr = self.run_cli("list")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("5h 95% left", stdout)
        self.assertIn("weekly 70% left", stdout)
        self.assertIn("local history 4.2K (100.0%, 1 thread)", stdout)

    def test_list_fresh_fetches_live_quota_for_saved_accounts(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com", plan_type="free")
        self.service.add_current_account("personal")

        requests: list[tuple[str, dict[str, str] | None, dict | None]] = []

        def fake_json_request(
            url: str,
            *,
            headers: dict[str, str] | None = None,
            payload: dict | None = None,
            timeout_seconds: int = 15,
        ) -> dict:
            del timeout_seconds
            requests.append((url, headers, payload))
            return {
                "plan_type": "plus",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 8,
                        "limit_window_seconds": 18_000,
                        "reset_at": 1_773_969_965,
                    },
                    "secondary_window": {
                        "used_percent": 87,
                        "limit_window_seconds": 604_800,
                        "reset_at": 1_774_569_999,
                    },
                },
                "credits": {
                    "has_credits": True,
                    "unlimited": False,
                    "balance": "12",
                },
            }

        with patch("gpt_switcher.core.json_request", side_effect=fake_json_request):
            exit_code, stdout, stderr = self.run_cli("list", "--fresh")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("plus", stdout)
        self.assertIn("5h 92% left; weekly 13% left; credits 12", stdout)
        self.assertNotIn("fresh fetch failed", stdout)
        self.assertEqual(requests[0][0], "https://chatgpt.com/backend-api/wham/usage")
        assert requests[0][1] is not None
        self.assertEqual(requests[0][1]["ChatGPT-Account-Id"], "account-1")

    def test_list_fresh_retries_after_unauthorized_for_inactive_account_and_updates_snapshot(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        saved = self.service.add_current_account("personal")
        write_auth_file(self.paths.auth_path, "account-2", "two@example.com")
        self.service.add_current_account("work")

        refreshed_access_token = build_access_token("account-1", "one@example.com")
        calls: list[tuple[str, dict[str, str] | None, dict | None]] = []

        def fake_json_request(
            url: str,
            *,
            headers: dict[str, str] | None = None,
            payload: dict | None = None,
            timeout_seconds: int = 15,
        ) -> dict:
            del timeout_seconds
            calls.append((url, headers, payload))
            if (
                url.endswith("/wham/usage")
                and headers is not None
                and headers["ChatGPT-Account-Id"] == "account-1"
                and len(
                    [
                        call
                        for call in calls
                        if call[0].endswith("/wham/usage")
                        and call[1] is not None
                        and call[1]["ChatGPT-Account-Id"] == "account-1"
                    ]
                )
                == 1
            ):
                raise LiveQuotaUnauthorizedError("expired")
            if url.endswith("/oauth/token"):
                return {
                    "access_token": refreshed_access_token,
                    "refresh_token": "new-refresh-token",
                }
            if url.endswith("/wham/usage"):
                assert headers is not None
                if headers["ChatGPT-Account-Id"] == "account-1":
                    self.assertEqual(headers["Authorization"], f"Bearer {refreshed_access_token}")
                return {
                    "plan_type": "plus",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {
                            "used_percent": 10,
                            "limit_window_seconds": 18_000,
                            "reset_at": 1_773_969_965,
                        },
                    },
                }
            raise AssertionError(f"unexpected request {url}")

        with patch("gpt_switcher.core.json_request", side_effect=fake_json_request):
            exit_code, stdout, stderr = self.run_cli("list", "--fresh")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("personal", stdout)
        self.assertIn("work", stdout)

        auth_payload, _ = read_json_file(self.paths.auth_path)
        self.assertEqual(extract_auth_metadata(auth_payload).account_id, "account-2")

        snapshot_payload, _ = read_json_file(self.paths.accounts_dir / saved.snapshot_name)
        self.assertEqual(snapshot_payload["tokens"]["access_token"], refreshed_access_token)
        self.assertEqual(snapshot_payload["tokens"]["refresh_token"], "new-refresh-token")

    def test_list_fresh_marks_fallback_when_live_fetch_fails(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")
        create_logs_db(self.paths.logs_path)
        insert_log(
            self.paths.logs_path,
            ts=100,
            target="log",
            message=request_message("account-1"),
            thread_id="thread-1",
        )
        insert_log(
            self.paths.logs_path,
            ts=110,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 5,
                            "window_minutes": 300,
                            "reset_at": 1_773_969_965,
                        },
                        "secondary": {
                            "used_percent": 30,
                            "window_minutes": 10_080,
                            "reset_at": 1_774_569_999,
                        },
                    },
                }
            ),
            thread_id="thread-1",
        )

        with patch("gpt_switcher.core.json_request", side_effect=SwitcherError("network down")):
            exit_code, stdout, stderr = self.run_cli("list", "--fresh")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("5h 95% left; weekly 70% left; fresh fetch failed", stdout)

    def test_local_usage_summary_shows_percentage_share(self) -> None:
        create_logs_db(self.paths.logs_path)
        create_state_db(self.paths.state_path)
        insert_log(
            self.paths.logs_path,
            ts=500,
            target="log",
            message=request_message("account-a"),
            thread_id="thread-a",
        )
        insert_log(
            self.paths.logs_path,
            ts=501,
            target="log",
            message=request_message("account-b"),
            thread_id="thread-b",
        )
        insert_thread(self.paths.state_path, thread_id="thread-a", tokens_used=100, updated_at=1_700_000_600)
        insert_thread(self.paths.state_path, thread_id="thread-b", tokens_used=300, updated_at=1_700_000_700)

        usage = load_usage_by_account(self.paths.logs_path, self.paths.state_path)

        summary = summarize_usage(usage["account-a"])
        self.assertIn("local history", summary)
        self.assertIn("100", summary)
        self.assertIn("25.0%", summary)

    def test_usage_parser_supports_feedback_log_body_schema(self) -> None:
        create_logs_db(self.paths.logs_path, body_column="feedback_log_body")
        insert_log(
            self.paths.logs_path,
            ts=400,
            target="log",
            message=request_message("account-schema"),
            thread_id="thread-schema",
            body_column="feedback_log_body",
        )
        insert_log(
            self.paths.logs_path,
            ts=401,
            target="codex_api::endpoint::responses_websocket",
            message=websocket_event(
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary": {
                            "used_percent": 7,
                            "window_minutes": 300,
                            "reset_at": 1775000000,
                        },
                        "secondary": {
                            "used_percent": 11,
                            "window_minutes": 10080,
                            "reset_at": 1775600000,
                        },
                    },
                    "credits": None,
                }
            ),
            thread_id="thread-schema",
            body_column="feedback_log_body",
        )

        usage = load_latest_usage_by_account(self.paths.logs_path)

        snapshot = usage["account-schema"]
        self.assertEqual(snapshot.primary_used_percent, 7)
        self.assertEqual(snapshot.secondary_used_percent, 11)


if __name__ == "__main__":
    unittest.main()
