from __future__ import annotations

import unittest
from datetime import datetime, timezone

from research_sim.bot.app import (
    format_moscow_time,
    is_admin,
    parse_admin_ids,
    parse_day_slots,
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


if __name__ == "__main__":
    unittest.main()