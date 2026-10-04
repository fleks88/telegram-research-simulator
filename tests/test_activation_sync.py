from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from research_sim.database import Database, DatabaseRequests
from research_sim.services.activation_sync import ActivationSyncService
from research_sim.settings import Settings


PACKS = (8100, 3650, 1800, 660, 325, 60)


def make_snapshot(as_of: datetime, delta_8100: int = 0) -> Dict[str, Any]:
    return {
        "as_of": as_of.isoformat(),
        "window_hours": 24,
        "packs": [
            {
                "pack_size": pack_size,
                "activations_24h": 100 + (delta_8100 if pack_size == 8100 else 0),
                "activations_since_previous_sync": delta_8100 if pack_size == 8100 else 0,
            }
            for pack_size in PACKS
        ],
    }


class FakeMessaging:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    async def send_message(
        self,
        recipient: str,
        text: str,
        *,
        apply_persona: bool = False,
    ) -> Any:
        self.sent.append((recipient, text, apply_persona))
        return type("SendResult", (), {"sender_account": "business"})()


class ActivationSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_snapshot_is_baseline_and_8100x10_sends_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "activation.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.save_campaign_settings(
                {
                    "campaign_id": "activation-research",
                    "enabled": False,
                    "activation_enabled": True,
                    "activation_rules": {"8100": 10},
                    "recipient": "central_user",
                    "phrases": ["Pack {pack}: threshold {activations}"],
                    "start_date": "2026-10-01",
                    "day_slots": {"1": ["10:00"]},
                }
            )
            settings = Settings(
                database_path=Path(directory) / "activation.sqlite3",
                api_token="api-token",
                telegram_api_id=1,
                telegram_api_hash="hash",
                telegram_session_dir=Path(directory) / "sessions",
                allowed_recipients={"central_user"},
                minimum_send_interval_seconds=0,
                pack_activation_endpoint="https://example.invalid/activations",
            )
            first_at = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
            snapshots = [
                make_snapshot(first_at, delta_8100=500_000),
                make_snapshot(first_at + timedelta(minutes=5), delta_8100=81_000),
                make_snapshot(first_at + timedelta(minutes=10), delta_8100=0),
            ]

            async def fetcher(**kwargs: Any) -> Dict[str, Any]:
                return snapshots.pop(0)

            messaging = FakeMessaging()
            service = ActivationSyncService(
                settings,
                requests,
                messaging,
                fetcher=fetcher,
            )
            initial = await service.sync_once(now=first_at.timestamp())
            self.assertEqual(initial["status"], "baseline_saved")
            self.assertEqual(messaging.sent, [])

            triggered = await service.sync_once(
                now=(first_at + timedelta(minutes=5)).timestamp()
            )
            self.assertEqual(triggered["status"], "sent")
            self.assertEqual(triggered["pack_size"], 8100)
            self.assertEqual(triggered["threshold"], 81000)
            self.assertEqual(
                messaging.sent,
                [("central_user", "Pack 8100: threshold 81000", True)],
            )

            no_repeat = await service.sync_once(
                now=(first_at + timedelta(minutes=10)).timestamp()
            )
            self.assertEqual(no_repeat["status"], "synced")
            self.assertEqual(len(messaging.sent), 1)

            state = requests.get_activation_sync_state()
            self.assertEqual(state["first_response"]["as_of"], first_at.isoformat())
            self.assertEqual(
                state["last_response"]["as_of"],
                (first_at + timedelta(minutes=10)).isoformat(),
            )
            self.assertEqual(state["remainders"]["8100x10"], 0)


if __name__ == "__main__":
    unittest.main()