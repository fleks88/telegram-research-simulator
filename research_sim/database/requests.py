from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

from .connection import Database
from .models import (
    CampaignSettingsRecord,
    DeliveryReservation,
    SenderAccount,
    SenderAccountReservation,
)


class DatabaseRequests:
    """All application SQL lives here; services use named operations only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get_campaign_settings(self) -> Optional[CampaignSettingsRecord]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT config_json FROM campaign_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return CampaignSettingsRecord(config=json.loads(row["config_json"]))

    def get_sender_persona(self, account_key: str) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT persona_json FROM sender_personas WHERE account_key = ?",
                (account_key,),
            ).fetchone()
        return json.loads(row["persona_json"]) if row is not None else None

    def save_sender_persona(
        self,
        account_key: str,
        persona: Dict[str, Any],
    ) -> bool:
        now = time.time()
        serialized = json.dumps(persona, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM telegram_accounts WHERE account_key = ?",
                (account_key,),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                """INSERT INTO sender_personas (account_key, persona_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(account_key) DO UPDATE SET
                       persona_json = excluded.persona_json,
                       updated_at = excluded.updated_at""",
                (account_key, serialized, now),
            )
            return True

    def save_dialogue_proposal(
        self,
        account_key: str,
        prompt: str,
        dialogue: list[Dict[str, str]],
    ) -> int:
        now = time.time()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO dialogue_proposals
                   (account_key, prompt, dialogue_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    account_key,
                    prompt,
                    json.dumps(dialogue, ensure_ascii=False),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_dialogue_proposals(
        self,
        account_key: str,
        *,
        limit: int = 10,
    ) -> list[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, account_key, prompt, dialogue_json, created_at
                   FROM dialogue_proposals WHERE account_key = ?
                   ORDER BY id DESC LIMIT ?""",
                (account_key, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "account_key": row["account_key"],
                "prompt": row["prompt"],
                "dialogue": json.loads(row["dialogue_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_campaign_settings(self, config: Dict[str, Any]) -> None:
        now = time.time()
        serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO campaign_settings (id, config_json, updated_at)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       config_json = excluded.config_json,
                       updated_at = excluded.updated_at""",
                (serialized, now),
            )

    def get_activation_sync_state(self) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM activation_sync_state WHERE id = 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row is not None else None

    def save_activation_sync_state(self, state: Dict[str, Any]) -> None:
        now = time.time()
        serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO activation_sync_state (id, state_json, updated_at)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       state_json = excluded.state_json,
                       updated_at = excluded.updated_at""",
                (serialized, now),
            )

    def claim_received_message(
        self,
        *,
        account_key: str,
        telegram_message_id: int,
        sender_id: int,
        message_text: str,
    ) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO received_messages
                   (account_key, telegram_message_id, sender_id, message_text, received_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    account_key,
                    telegram_message_id,
                    sender_id,
                    message_text,
                    time.time(),
                ),
            )
            return cursor.rowcount == 1

    def set_received_message_status(
        self,
        account_key: str,
        telegram_message_id: int,
        status: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE received_messages SET reply_status = ?
                   WHERE account_key = ? AND telegram_message_id = ?""",
                (status, account_key, telegram_message_id),
            )

    def enqueue_auto_reply(
        self,
        *,
        account_key: str,
        telegram_message_id: int,
        due_at: float,
        reply_text: str,
    ) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO pending_auto_replies
                   (account_key, telegram_message_id, due_at, reply_text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (account_key, telegram_message_id, due_at, reply_text, time.time()),
            )
            return cursor.rowcount == 1

    def claim_due_auto_replies(
        self,
        *,
        now: Optional[float] = None,
        limit: int = 10,
    ) -> list[Dict[str, Any]]:
        current = time.time() if now is None else now
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE pending_auto_replies
                   SET status = 'queued', processed_at = NULL
                   WHERE status = 'processing' AND processed_at < ?""",
                (current - 300,),
            )
            rows = connection.execute(
                """SELECT q.id, q.account_key, q.telegram_message_id, q.due_at,
                          q.reply_text, r.message_text
                   FROM pending_auto_replies q
                   JOIN received_messages r
                     ON r.account_key = q.account_key
                    AND r.telegram_message_id = q.telegram_message_id
                   WHERE q.status = 'queued' AND q.due_at <= ?
                   ORDER BY q.due_at, q.id LIMIT ?""",
                (current, limit),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                connection.execute(
                    f"UPDATE pending_auto_replies "
                    f"SET status = 'processing', processed_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (current, *(row["id"] for row in rows)),
                )
        return [dict(row) for row in rows]

    def finish_auto_reply(
        self,
        queue_id: int,
        *,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE pending_auto_replies
                   SET status = ?, processed_at = ?, error = ? WHERE id = ?""",
                (status, time.time(), error[:500] if error else None, queue_id),
            )

    def requeue_auto_reply(self, queue_id: int, *, due_at: float) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE pending_auto_replies
                   SET status = 'queued', due_at = ?, processed_at = NULL, error = NULL
                   WHERE id = ?""",
                (due_at, queue_id),
            )

    def list_pending_auto_replies(self, *, limit: int = 50) -> list[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, account_key, telegram_message_id, due_at, status,
                          created_at, processed_at, reply_text, error
                   FROM pending_auto_replies
                   WHERE status IN ('queued', 'processing')
                   ORDER BY due_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_account_timeline(
        self,
        account_key: str,
        *,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM (
                       SELECT 'incoming' AS kind, message_text, received_at AS created_at,
                              NULL AS due_at, reply_status AS status
                       FROM received_messages WHERE account_key = ?
                       UNION ALL
                       SELECT 'outgoing' AS kind, message_text, created_at,
                              NULL AS due_at, status
                       FROM message_deliveries WHERE sender_account = ?
                       UNION ALL
                       SELECT 'planned' AS kind, COALESCE(reply_text, '') AS message_text,
                              created_at, due_at, status
                       FROM pending_auto_replies
                       WHERE account_key = ? AND status IN ('queued', 'processing')
                   ) ORDER BY created_at DESC LIMIT ?""",
                (account_key, account_key, account_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation_context(
        self,
        account_key: str,
        *,
        limit: int = 12,
    ) -> list[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT direction, message_text, created_at FROM (
                       SELECT 'incoming' AS direction, message_text,
                              received_at AS created_at
                       FROM received_messages WHERE account_key = ?
                       UNION ALL
                       SELECT 'outgoing' AS direction, message_text, created_at
                       FROM message_deliveries
                       WHERE sender_account = ? AND status = 'sent'
                   ) ORDER BY created_at DESC LIMIT ?""",
                (account_key, account_key, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_sender_account(self, account_key: str, label: str) -> SenderAccount:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_accounts (account_key, label, created_at)
                   VALUES (?, ?, ?)""",
                (account_key, label, now),
            )
        return next(
            account
            for account in self.list_sender_accounts()
            if account.account_key == account_key
        )

    def list_sender_accounts(self) -> list[SenderAccount]:
        with self.database.connect() as connection:
            rows = connection.execute(
                     """SELECT account_key, label, enabled, send_count, last_used_at
                         FROM telegram_accounts ORDER BY id"""
            ).fetchall()
        return [
            SenderAccount(
                account_index=index,
                account_key=row["account_key"],
                label=row["label"],
                enabled=bool(row["enabled"]),
                send_count=int(row["send_count"]),
                last_used_at=row["last_used_at"],
            )
            for index, row in enumerate(rows, start=1)
        ]

    def reserve_sender_account_by_index(
        self,
        account_index: int,
    ) -> SenderAccountReservation:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT id, account_key, label, enabled, send_count, last_used_at
                   FROM telegram_accounts ORDER BY id LIMIT 1 OFFSET ?""",
                (account_index - 1,),
            ).fetchone()
            if row is None:
                return SenderAccountReservation(None, "not_found")
            if not row["enabled"]:
                return SenderAccountReservation(None, "disabled")
            return SenderAccountReservation(SenderAccount(
                account_index=account_index,
                account_key=row["account_key"],
                label=row["label"],
                enabled=True,
                send_count=int(row["send_count"]),
                last_used_at=row["last_used_at"],
            ))

    def set_sender_account_enabled(self, account_key: str, enabled: bool) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE telegram_accounts SET enabled = ? WHERE account_key = ?",
                (int(enabled), account_key),
            )
            return cursor.rowcount == 1

    def reserve_next_sender_account(self) -> SenderAccountReservation:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rotation = connection.execute(
                "SELECT last_account_key FROM account_rotation WHERE id = 1"
            ).fetchone()
            last_key = rotation["last_account_key"] if rotation is not None else None
            row = None
            if last_key is not None:
                row = connection.execute(
                          """SELECT id, account_key, label, enabled, send_count, last_used_at
                       FROM telegram_accounts
                              WHERE enabled = 1 AND id > (
                                    SELECT id FROM telegram_accounts WHERE account_key = ?
                              ) ORDER BY id LIMIT 1""",
                    (last_key,),
                ).fetchone()
            if row is None:
                row = connection.execute(
                          """SELECT id, account_key, label, enabled, send_count, last_used_at
                       FROM telegram_accounts
                              WHERE enabled = 1 ORDER BY id LIMIT 1"""
                ).fetchone()
            if row is None:
                return SenderAccountReservation(None, "not_found")

            connection.execute(
                """INSERT INTO account_rotation (id, last_account_key)
                   VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       last_account_key = excluded.last_account_key""",
                (row["account_key"],),
            )
            return SenderAccountReservation(SenderAccount(
                account_index=int(
                    connection.execute(
                        "SELECT COUNT(*) FROM telegram_accounts WHERE id <= ?",
                        (row["id"],),
                    ).fetchone()[0]
                ),
                account_key=row["account_key"],
                label=row["label"],
                enabled=bool(row["enabled"]),
                send_count=int(row["send_count"]),
                last_used_at=row["last_used_at"],
            ))

    def record_sender_account_success(self, account_key: str) -> None:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE telegram_accounts
                   SET send_count = send_count + 1, last_used_at = ?
                   WHERE account_key = ?""",
                (now, account_key),
            )

    def reserve_delivery(
        self,
        *,
        recipient: str,
        minimum_interval_seconds: int,
        campaign_id: Optional[str] = None,
        campaign_day: Optional[int] = None,
        campaign_slot: Optional[str] = None,
    ) -> DeliveryReservation:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if campaign_id is not None:
                existing = connection.execute(
                    """SELECT id FROM message_deliveries
                       WHERE campaign_id = ? AND campaign_day = ? AND campaign_slot = ?""",
                    (campaign_id, campaign_day, campaign_slot),
                ).fetchone()
                if existing is not None:
                    return DeliveryReservation(None, "already_processed")

            last_delivery = connection.execute(
                """SELECT MAX(created_at) AS created_at FROM message_deliveries
                   WHERE recipient = ?""",
                (recipient,),
            ).fetchone()
            last_created_at = last_delivery["created_at"]
            if (
                last_created_at is not None
                and now - float(last_created_at) < minimum_interval_seconds
            ):
                return DeliveryReservation(None, "rate_limited")

            try:
                cursor = connection.execute(
                    """INSERT INTO message_deliveries
                       (recipient, created_at, status, campaign_id, campaign_day, campaign_slot)
                       VALUES (?, ?, 'reserved', ?, ?, ?)""",
                    (recipient, now, campaign_id, campaign_day, campaign_slot),
                )
            except sqlite3.IntegrityError:
                return DeliveryReservation(None, "already_processed")
            return DeliveryReservation(int(cursor.lastrowid))

    def finish_delivery(
        self,
        delivery_id: int,
        *,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE message_deliveries SET status = ?, error = ?
                   WHERE id = ?""",
                (status, error[:500] if error else None, delivery_id),
            )

    def set_delivery_content(
        self,
        delivery_id: int,
        *,
        sender_account: str,
        message_text: str,
        photo_attached: bool,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE message_deliveries
                   SET sender_account = ?, message_text = ?, photo_attached = ?
                   WHERE id = ?""",
                (sender_account, message_text, int(photo_attached), delivery_id),
            )

    def get_delivery(self, delivery_id: int) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                     """SELECT id, recipient, sender_account, message_text,
                                  photo_attached, created_at, status, error
                   FROM message_deliveries WHERE id = ?""",
                (delivery_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_account_history(
        self,
        account_key: str,
        *,
        limit: int,
        before_id: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        with self.database.connect() as connection:
            if before_id is None:
                rows = connection.execute(
                    """SELECT id, sender_account, recipient, message_text,
                              photo_attached, created_at, status, error
                       FROM message_deliveries
                       WHERE sender_account = ?
                       ORDER BY id DESC LIMIT ?""",
                    (account_key, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT id, sender_account, recipient, message_text,
                              photo_attached, created_at, status, error
                       FROM message_deliveries
                       WHERE sender_account = ? AND id < ?
                       ORDER BY id DESC LIMIT ?""",
                    (account_key, before_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
