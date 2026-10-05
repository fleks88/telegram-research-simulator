from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from research_sim.bot.app import (
    format_moscow_time,
    is_admin,
    parse_activation_rules,
    parse_activation_rules,
    parse_admin_ids,
    parse_day_slots,
    save_session_bundle,
    validate_session_upload,
)


class ControlBotHelpersTest(unittest.TestCase):
    def test_history_time_is_rendered_in_moscow_timezone(self) -> None:
        timestamp = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(format_moscow_time(timestamp), "03.10.2026 12:00 МСК")

    def test_only_configured_admin_ids_are_allowed(self) -> None:
        admin_ids = parse_admin_ids("12345, 67890")
        self.assertTrue(is_admin(12345, admin_ids))
        self.assertFalse(is_admin(11111, admin_ids))
        self.assertFalse(is_admin(None, admin_ids))

    def test_schedule_parser_accepts_three_day_moscow_slots(self) -> None:
        self.assertEqual(
            parse_day_slots("10:00 | 10:00,15:00 | 10:30"),
            {"1": ["10:00"], "2": ["10:00", "15:00"], "3": ["10:30"]},
        )

    def test_schedule_parser_rejects_close_slots(self) -> None:
        with self.assertRaisesRegex(ValueError, "30 минут"):
            parse_day_slots("10:00,10:15 | 15:00 | 10:00")

    def test_activation_rule_parser_supports_multipliers(self) -> None:
        self.assertEqual(
            parse_activation_rules("8100х10,3650x0,60x4"),
            {"8100": 10, "60": 4},
        )
        with self.assertRaisesRegex(ValueError, "больше одного раза"):
            parse_activation_rules("8100x10,8100x2")

    def test_activation_rules_parse_pack_multipliers(self) -> None:
        self.assertEqual(
            parse_activation_rules("8100х10,3650x4,1800x0"),
            {"8100": 10, "3650": 4},
        )

    def test_activation_rules_reject_duplicates_and_unknown_packs(self) -> None:
        with self.assertRaisesRegex(ValueError, "больше одного раза"):
            parse_activation_rules("8100x10,8100x4")
        with self.assertRaisesRegex(ValueError, "Неизвестный пакет"):
            parse_activation_rules("999x10")

    def test_session_bundle_upload_is_validated_and_saved_privately(self) -> None:
        session = b"SQLite format 3\x00" + b"test"
        metadata = b'{"api_id": 123, "api_hash": "hash"}'
        self.assertEqual(
            validate_session_upload("Research_A.session", session),
            ("research_a", ".session"),
        )
        self.assertEqual(
            validate_session_upload("Research_A.json", metadata),
            ("research_a", ".json"),
        )
        self.assertEqual(
            validate_session_upload("+79990001122.session", session),
            ("79990001122", ".session"),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sessions"
            save_session_bundle(
                target,
                "research_a",
                {".session": session, ".json": metadata},
            )
            self.assertEqual((target / "research_a.session").read_bytes(), session)
            self.assertEqual((target / "research_a.json").read_bytes(), metadata)
            self.assertEqual((target / "research_a.session").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                save_session_bundle(
                    target,
                    "research_a",
                    {".session": session, ".json": metadata},
                )


if __name__ == "__main__":
    unittest.main()
