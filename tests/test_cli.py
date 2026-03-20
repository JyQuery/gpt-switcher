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
    SwitcherError,
    SwitcherService,
    extract_auth_metadata,
    load_latest_usage_by_account,
    load_usage_by_account,
    read_json_file,
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


def websocket_event(payload: dict) -> str:
    return "websocket event: " + json.dumps(payload, separators=(",", ":"))


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
            exit_code = main(list(argv))
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

    def test_status_reports_unmanaged_current_account(self) -> None:
        write_auth_file(self.paths.auth_path, "account-9", "unmanaged@example.com")

        exit_code, stdout, stderr = self.run_cli("status")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Label: unmanaged", stdout)
        self.assertIn("Email: unmanaged@example.com", stdout)

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

    def test_list_shows_unknown_usage_when_no_logs_exist(self) -> None:
        write_auth_file(self.paths.auth_path, "account-1", "one@example.com")
        self.service.add_current_account("personal")

        exit_code, stdout, stderr = self.run_cli("list")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("personal", stdout)
        self.assertIn("unknown", stdout)

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

        snapshot = usage["account-local"]
        self.assertEqual(snapshot.source, "local_threads")
        self.assertEqual(snapshot.local_tokens_used, 3500)
        self.assertEqual(snapshot.local_thread_count, 2)
        self.assertEqual(snapshot.observed_at, 777)

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
