#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'Error: run this script from a cloned Git repository.\n' >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  printf 'Error: tracked files have local changes. Commit/stash them before deploy.\n' >&2
  exit 1
fi

printf 'Pulling latest repository changes...\n'
git pull --ff-only

if ! command -v python3 >/dev/null 2>&1; then
  printf 'Error: python3 is required. Install Python 3.9+ first.\n' >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/pip" install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
fi
chmod 600 .env

"$ROOT/.venv/bin/python" - <<'PY'
from __future__ import annotations

import getpass
import re
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

env_path = Path(".env")


def current_value(key: str) -> str:
    value = dotenv_values(env_path).get(key)
    return value.strip() if value else ""


def ask_value(key: str, label: str, *, secret: bool = False) -> str:
    existing = current_value(key)
    if existing:
        return existing
    while True:
        prompt = f"{label}: "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if value:
            set_key(str(env_path), key, value, quote_mode="always")
            return value
        print("This value is required.")


def ask_optional_secret(key: str, label: str) -> None:
    if current_value(key):
        return
    value = getpass.getpass(f"{label} (Enter to skip): ").strip()
    if value:
        set_key(str(env_path), key, value, quote_mode="always")


ask_value("CONTROL_BOT_TOKEN", "Token from @BotFather", secret=True)
admin_ids = ask_value("CONTROL_ADMIN_IDS", "Admin Telegram numeric user ID(s), comma-separated")
if not all(re.fullmatch(r"[1-9][0-9]*", item.strip()) for item in admin_ids.split(",")):
    raise SystemExit("CONTROL_ADMIN_IDS must contain positive numeric IDs separated by commas")

api_token = current_value("API_TOKEN")
if not api_token:
    api_token = secrets.token_hex(32)
    set_key(str(env_path), "API_TOKEN", api_token, quote_mode="always")
    print("Generated API_TOKEN and saved it to .env.")

api_id = ask_value("TELEGRAM_API_ID", "Telegram API ID from my.telegram.org")
if not api_id.isdigit():
    raise SystemExit("TELEGRAM_API_ID must be numeric")
ask_value("TELEGRAM_API_HASH", "Telegram API hash", secret=True)
recipient = ask_value("TELEGRAM_ALLOWED_RECIPIENTS", "Central test username, without @")
if "," in recipient:
    raise SystemExit("Configure exactly one central account in TELEGRAM_ALLOWED_RECIPIENTS")

ask_optional_secret("LLM_API_KEY", "LLM API key (optional; needed only for auto-replies)")
PY

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  printf 'Node.js and npm are required for PM2. Install them, then run deploy.sh again.\n' >&2
  exit 1
fi

if ! command -v pm2 >/dev/null 2>&1; then
  printf 'Installing PM2...\n'
  if [[ "$(id -u)" -eq 0 ]]; then
    npm install --global pm2
  else
    sudo npm install --global pm2
  fi
fi

printf 'Starting/restarting API and control bot with PM2...\n'
pm2 startOrRestart "$ROOT/ecosystem.config.cjs" --update-env
pm2 save
pm2 status

printf '\nDeploy finished. Check the API with: curl http://127.0.0.1:8000/health\n'
printf 'View logs with: pm2 logs telegram-research-api\n'
printf 'View bot logs with: pm2 logs telegram-research-bot\n'
printf 'Open your Telegram bot and send /start.\n'
printf 'For reboot persistence, run `pm2 startup`, execute its printed command, then `pm2 save`.\n'
printf 'Telethon session files must be authorized separately before /add_account can use them.\n'