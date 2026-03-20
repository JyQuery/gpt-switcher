# gpt-switcher

`gpt-switcher` is a small Python CLI that lets you save multiple pre-logged-in Codex ChatGPT accounts and switch the active `~/.codex/auth.json` without running the OAuth flow every time.

It also shows the latest Codex usage/rate-limit snapshot that your local Codex logs have seen for each saved account.

## Requirements

- Python 3.13+
- Codex CLI already installed
- File-based Codex auth cache at `~/.codex/auth.json`

If Codex is storing auth in the OS credential store instead of `auth.json`, add this to `~/.codex/config.toml` and sign in again:

```toml
cli_auth_credentials_store = "file"
```

## Install

```bash
python -m pip install -e .
```

## Usage

1. Log into the first account with `codex login`.
2. Save it with `gpt-switcher add <label>`.
3. Log into the next account with `codex login`.
4. Save it with another label.
5. Switch any time with `gpt-switcher switch <label>`.

Examples:

```bash
gpt-switcher add personal
gpt-switcher add work
gpt-switcher list
gpt-switcher status
gpt-switcher switch work
```

## Commands

- `gpt-switcher add <label>`: save the current `~/.codex/auth.json` under a label
- `gpt-switcher list`: list saved accounts and the latest known usage snapshot for each
- `gpt-switcher status`: show the active Codex account and whether it is managed by `gpt-switcher`
- `gpt-switcher switch <label>`: replace the active `~/.codex/auth.json` with the saved snapshot for that label

## Storage

- Managed account snapshots live in `~/.gpt-switcher/accounts/`
- Metadata lives in `~/.gpt-switcher/registry.json`

You can override the default locations with:

- `CODEX_HOME`
- `GPT_SWITCHER_HOME`

## Usage Data

The displayed usage prefers the latest Codex rate-limit snapshot already present in `~/.codex/logs_1.sqlite`. If that snapshot is missing for an account, `gpt-switcher` falls back to local per-account token totals inferred from Codex thread history in `~/.codex/state_5.sqlite`.

If no matching usage event is present yet for an account, `gpt-switcher` shows `unknown`.
