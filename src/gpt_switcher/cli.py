from __future__ import annotations

import argparse
import sys

from .core import (
    AppPaths,
    SwitcherError,
    SwitcherService,
    format_timestamp,
    render_table,
    short_account_id,
    summarize_usage,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt-switcher",
        description="Switch between saved Codex ChatGPT accounts without re-running OAuth.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Save the current Codex account under a label.")
    add_parser.add_argument("label", help="Human-friendly label for the current account.")

    subparsers.add_parser("list", help="List saved accounts and their last-known Codex usage.")

    switch_parser = subparsers.add_parser("switch", help="Make a saved account the active Codex login.")
    switch_parser.add_argument("label", help="Label of the saved account to activate.")

    subparsers.add_parser("status", help="Show the active Codex account and whether it is managed.")
    return parser


def command_add(service: SwitcherService, label: str) -> int:
    account = service.add_current_account(label)
    print(f"Saved '{account.label}' -> {account.email} ({account.plan_type}, {account.account_id}).")
    return 0


def command_list(service: SwitcherService) -> int:
    accounts = service.list_accounts()
    if not accounts:
        print("No saved accounts. Run `codex login` and then `gpt-switcher add <label>`.")
        return 0

    active = service.try_read_current_auth()
    usage_by_account = service.load_usage()
    rows: list[list[str]] = []
    for account in accounts:
        active_marker = "*" if active is not None and active.account_id == account.account_id else ""
        rows.append(
            [
                active_marker,
                account.label,
                account.email,
                account.plan_type,
                short_account_id(account.account_id),
                summarize_usage(usage_by_account.get(account.account_id)),
            ]
        )

    print(render_table(["ACTIVE", "LABEL", "EMAIL", "PLAN", "ACCOUNT", "USAGE"], rows))
    return 0


def command_switch(service: SwitcherService, label: str) -> int:
    account = service.switch_account(label)
    print(
        f"Switched active Codex auth to '{account.label}' -> {account.email} ({account.plan_type}, {account.account_id})."
    )
    return 0


def command_status(service: SwitcherService) -> int:
    status = service.get_active_status()
    if status.metadata is None:
        print(f"No active Codex auth.json found at {service.paths.auth_path}.")
        return 0

    print("Active Codex account")
    print(f"Label: {status.saved_account.label if status.saved_account is not None else 'unmanaged'}")
    print(f"Email: {status.metadata.email}")
    print(f"Plan: {status.metadata.plan_type}")
    print(f"Account ID: {status.metadata.account_id}")
    if status.metadata.token_expires_at is not None:
        print(f"Access token expires: {format_timestamp(status.metadata.token_expires_at)}")
    print(f"Usage: {summarize_usage(status.usage)}")
    if status.usage is not None:
        print(f"Usage observed: {format_timestamp(status.usage.observed_at)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = SwitcherService(AppPaths.from_env())

    try:
        if args.command == "add":
            return command_add(service, args.label)
        if args.command == "list":
            return command_list(service)
        if args.command == "switch":
            return command_switch(service, args.label)
        if args.command == "status":
            return command_status(service)
    except SwitcherError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
