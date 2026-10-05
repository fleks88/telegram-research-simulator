from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS campaign_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'sent', 'failed')),
    error TEXT,
    sender_account TEXT,
    message_text TEXT NOT NULL DEFAULT '',
    photo_attached INTEGER NOT NULL DEFAULT 0 CHECK (photo_attached IN (0, 1)),
    campaign_id TEXT,
    campaign_day INTEGER,
    campaign_slot TEXT
);

CREATE TABLE IF NOT EXISTS telegram_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    send_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS account_rotation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_account_key TEXT
);

CREATE TABLE IF NOT EXISTS received_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    received_at REAL NOT NULL,
    reply_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (reply_status IN ('pending', 'replied', 'ignored', 'failed')),
    UNIQUE (account_key, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS pending_auto_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    due_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'sent', 'failed', 'cancelled')),
    created_at REAL NOT NULL,
    processed_at REAL,
    reply_text TEXT,
    unknown_term TEXT,
    error TEXT,
    UNIQUE (account_key, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS sender_personas (
    account_key TEXT PRIMARY KEY REFERENCES telegram_accounts(account_key) ON DELETE CASCADE,
    persona_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dialogue_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL REFERENCES telegram_accounts(account_key),
    prompt TEXT NOT NULL,
    dialogue_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activation_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_terms (
    term_key TEXT PRIMARY KEY,
    display_term TEXT NOT NULL,
    definition TEXT NOT NULL,
    source_account_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_term_questions (
    account_key TEXT PRIMARY KEY REFERENCES telegram_accounts(account_key) ON DELETE CASCADE,
    term_key TEXT NOT NULL,
    display_term TEXT NOT NULL,
    asked_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_delivery_slot
ON message_deliveries(campaign_id, campaign_day, campaign_slot)
WHERE campaign_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_message_delivery_recipient_time
ON message_deliveries(recipient, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_auto_replies_due
ON pending_auto_replies(status, due_at);

"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(message_deliveries)"
                ).fetchall()
            }
            migrations = {
                "sender_account": "ALTER TABLE message_deliveries ADD COLUMN sender_account TEXT",
                "message_text": "ALTER TABLE message_deliveries ADD COLUMN message_text TEXT NOT NULL DEFAULT ''",
                "photo_attached": "ALTER TABLE message_deliveries ADD COLUMN photo_attached INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            pending_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(pending_auto_replies)"
                ).fetchall()
            }
            if "reply_text" not in pending_columns:
                connection.execute(
                    "ALTER TABLE pending_auto_replies ADD COLUMN reply_text TEXT"
                )
            if "unknown_term" not in pending_columns:
                connection.execute(
                    "ALTER TABLE pending_auto_replies ADD COLUMN unknown_term TEXT"
                )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_message_delivery_sender_time
                   ON message_deliveries(sender_account, created_at DESC, id DESC)"""
            )
