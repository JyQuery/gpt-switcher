# gpt-switcher

`gpt-switcher` is a small Python CLI that lets you save multiple pre-logged-in Codex ChatGPT accounts and switch the active `~/.codex/auth.json` without running the OAuth flow every time.

## Requirements

- Python 3.14+
- Codex CLI already installed

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
gpt-switcher status # Get fresh data for the current active account.
gpt-switcher list --fresh # Get fresh daily/weekly limit for all accounts.
gpt-switcher switch work
```

## Storage

- Managed account snapshots live in `~/.gpt-switcher/accounts/`
- Metadata lives in `~/.gpt-switcher/registry.json`

You can override the default locations with:

- `CODEX_HOME`
- `GPT_SWITCHER_HOME`
