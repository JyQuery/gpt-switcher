from __future__ import annotations

import argparse
import sys

from .core import (
    AppPaths,
    SwitcherError,
    SwitcherService,
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

    list_parser = subparsers.add_parser("list", help="List saved accounts with last-known Codex quota snapshots and local history.")
    list_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Fetch live ChatGPT quota for each saved account before printing the list.",
    )

    switch_parser = subparsers.add_parser("switch", help="Make a saved account the active Codex login.")
    switch_parser.add_argument("label", help="Label of the saved account to activate.")

    return parser


def command_add(service: SwitcherService, label: str) -> int:
    account = service.add_current_account(label)
    print(f"Saved '{account.label}' -> {account.email} ({account.plan_type}, {account.account_id}).")
    return 0


def command_list(service: SwitcherService, fresh: bool = False) -> int:
    accounts = service.list_accounts()
    if not accounts:
        print("No saved accounts. Run `codex login` and then `gpt-switcher add <label>`.")
        return 0

    usage_by_account = service.load_usage(fresh=fresh, accounts=accounts)
    if fresh:
        accounts = service.list_accounts()
    active = service.try_read_current_auth()
    active_auth_observed_at = service.active_auth_observed_at()
    if active is not None:
        active_usage = service.augment_usage_with_active_history(active, usage_by_account.get(active.account_id))
        if active_usage is not None:
            usage_by_account = dict(usage_by_account)
            usage_by_account[active.account_id] = active_usage

    rows: list[list[str]] = []
    for account in accounts:
        active_marker = "*" if active is not None and active.account_id == account.account_id else ""
        usage = usage_by_account.get(account.account_id)
        email = active.email if active is not None and active.account_id == account.account_id else account.email
        plan = account.plan_type
        if usage is not None and usage.quota_snapshot is not None and usage.quota_snapshot.plan_type:
            plan = usage.quota_snapshot.plan_type
        if (
            active is not None
            and active.account_id == account.account_id
            and active.plan_type != "unknown"
            and (
                usage is None
                or usage.quota_snapshot is None
                or usage.quota_snapshot.source != "live_rate_limits"
                or usage.quota_snapshot.plan_type is None
            )
            and (
                usage is None
                or usage.quota_snapshot is None
                or usage.quota_snapshot.plan_type is None
                or active_auth_observed_at is None
                or active_auth_observed_at >= usage.quota_snapshot.observed_at
            )
        ):
            plan = active.plan_type

        rows.append(
            [
                active_marker,
                account.label,
                email,
                plan,
                short_account_id(account.account_id),
                render_usage_text(usage, fresh=fresh),
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


def render_usage_text(usage, *, fresh: bool = False) -> str:
    summary = summarize_usage(usage)
    if fresh and usage is not None and usage.quota_refresh_error is not None:
        if summary == "unknown":
            return "fresh fetch failed"
        return f"{summary}; fresh fetch failed"
    return summary

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = SwitcherService(AppPaths.from_env())

    try:
        if args.command == "add":
            return command_add(service, args.label)
        if args.command == "list":
            return command_list(service, fresh=args.fresh)
        if args.command == "switch":
            return command_switch(service, args.label)
    except SwitcherError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
