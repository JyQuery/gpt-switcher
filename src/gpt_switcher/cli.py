from __future__ import annotations

import argparse
import sys

from .core import (
    AccountUsage,
    AppPaths,
    SavedAccount,
    SwitcherError,
    SwitcherService,
    format_timestamp,
    render_table,
    saved_account_key,
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

    subparsers.add_parser("status", help="Show the active Codex account with a fresh ChatGPT quota fetch.")

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
    active_saved_account = service.find_saved_account(active)
    active_auth_observed_at = service.active_auth_observed_at()
    if active is not None and active_saved_account is not None:
        active_usage = service.augment_usage_with_active_history(
            active,
            usage_by_account.get(saved_account_key(active_saved_account)),
        )
        if active_usage is not None:
            usage_by_account = dict(usage_by_account)
            usage_by_account[saved_account_key(active_saved_account)] = active_usage
    if fresh:
        accounts = sorted(
            accounts,
            key=lambda account: fresh_account_sort_key(account, usage_by_account.get(saved_account_key(account))),
        )

    rows: list[list[str]] = []
    for account in accounts:
        is_active_account = active_saved_account is not None and active_saved_account.label == account.label
        active_marker = "*" if is_active_account else ""
        usage = usage_by_account.get(saved_account_key(account))
        email = active.email if active is not None and is_active_account else account.email
        plan = account.plan_type
        if usage is not None and usage.quota_snapshot is not None and usage.quota_snapshot.plan_type:
            plan = usage.quota_snapshot.plan_type
        if (
            active is not None
            and is_active_account
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


def command_status(service: SwitcherService) -> int:
    status = service.get_active_status(refresh_quota=True)
    if status.metadata is None:
        print(f"No active Codex auth.json found at {service.paths.auth_path}.")
        return 0

    plan = status.metadata.plan_type
    if (
        status.usage is not None
        and status.usage.quota_snapshot is not None
        and status.usage.quota_snapshot.plan_type is not None
    ):
        plan = status.usage.quota_snapshot.plan_type

    print("Active Codex account")
    print(f"Label: {status.saved_account.label if status.saved_account is not None else 'unmanaged'}")
    print(f"Email: {status.metadata.email}")
    print(f"Plan: {plan}")
    print(f"Account ID: {status.metadata.account_id}")
    if status.metadata.token_expires_at is not None:
        print(f"Access token expires: {format_timestamp(status.metadata.token_expires_at)}")
    print(f"Usage: {summarize_usage(status.usage, include_live_reset_datetime=True)}")
    if status.quota_refresh_error is not None:
        print(f"Quota refresh: live fetch failed; {status.quota_refresh_error}")
    if status.usage is not None:
        if status.usage.quota_snapshot is not None:
            if status.usage.quota_snapshot.source == "live_rate_limits":
                print("Quota source: live ChatGPT rate limits fetch.")
            else:
                print("Quota source: last known Codex websocket snapshot, not a live ChatGPT quota fetch.")
            print(f"Quota observed: {format_timestamp(status.usage.quota_snapshot.observed_at)}")
        if status.usage.local_history_snapshot is not None:
            if status.usage.local_history_snapshot.source == "active_auth_local_threads":
                print("Local history source: threads updated since the current auth.json became active.")
            else:
                print("Local history source: local Codex thread history.")
            print(f"Local history observed: {format_timestamp(status.usage.local_history_snapshot.observed_at)}")
    return 0


def render_usage_text(usage, *, fresh: bool = False) -> str:
    summary = summarize_usage(usage, include_live_reset_datetime=fresh)
    if fresh and usage is not None and usage.quota_refresh_error is not None:
        if summary == "unknown":
            return "fresh fetch failed"
        return f"{summary}; fresh fetch failed"
    return summary


def fresh_account_sort_key(account: SavedAccount, usage: AccountUsage | None) -> tuple[int, int, int, str]:
    label_key = account.label.casefold()
    snapshot = usage.quota_snapshot if usage is not None else None
    if snapshot is None:
        return (2, sys.maxsize, sys.maxsize, label_key)

    windows: list[tuple[int | None, int | None]] = []
    for used_percent, reset_at in (
        (snapshot.primary_used_percent, snapshot.primary_reset_at),
        (snapshot.secondary_used_percent, snapshot.secondary_reset_at),
    ):
        if used_percent is None and reset_at is None:
            continue
        remaining_percent = None if used_percent is None else max(0, 100 - used_percent)
        windows.append((remaining_percent, reset_at))

    if not windows or any(remaining_percent is None for remaining_percent, _ in windows):
        return (2, sys.maxsize, sys.maxsize, label_key)

    remaining_values = [remaining_percent for remaining_percent, _ in windows if remaining_percent is not None]
    if all(remaining_percent > 0 for remaining_percent in remaining_values):
        earliest_reset = min(
            (reset_at for _, reset_at in windows if reset_at is not None),
            default=sys.maxsize,
        )
        return (0, -min(remaining_values), earliest_reset, label_key)

    exhausted_resets = [
        reset_at
        for remaining_percent, reset_at in windows
        if remaining_percent is not None and remaining_percent <= 0 and reset_at is not None
    ]
    if exhausted_resets:
        return (1, min(exhausted_resets), sys.maxsize, label_key)
    return (2, sys.maxsize, sys.maxsize, label_key)


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
        if args.command == "status":
            return command_status(service)
    except SwitcherError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
