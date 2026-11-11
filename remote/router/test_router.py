"""Tests for the priority plan of the reset-aware router."""

import unittest

from dataclasses import replace

from router import Seat, guard_until, plan, read_seat, state_of, window


def seat(name, week_used, week_hours_left, five_hour_used=0.0, unavailable=False):
    return Seat(name, name, None, unavailable, week_used, week_hours_left, five_hour_used)


class PlanTest(unittest.TestCase):
    def test_soonest_reset_wins_when_quota_is_equal(self):
        result = plan([seat("late", 0.2, 120), seat("soon", 0.2, 10)])
        self.assertGreater(result["soon"], result["late"])

    def test_more_quota_left_wins_when_reset_is_equal(self):
        result = plan([seat("used", 0.9, 24), seat("fresh", 0.1, 24)])
        self.assertGreater(result["fresh"], result["used"])

    def test_similar_seats_share_one_priority(self):
        result = plan([seat("a", 0.0, 126), seat("b", 0.0, 130), seat("c", 0.0, 400)])
        self.assertEqual(result["a"], result["b"])
        self.assertGreater(result["b"], result["c"])

    def test_cooler_five_hour_window_wins_between_equal_seats(self):
        result = plan([seat("hot", 0.2, 50, five_hour_used=0.7), seat("cool", 0.2, 50, five_hour_used=0.0)])
        self.assertGreater(result["cool"], result["hot"])

    def test_warm_seat_goes_below_each_open_seat(self):
        result = plan([seat("warm", 0.0, 2, five_hour_used=0.85), seat("open", 0.9, 160)])
        self.assertEqual(state_of(seat("warm", 0.0, 2, five_hour_used=0.85)), "warm")
        self.assertGreater(result["open"], result["warm"])

    def test_five_hour_full_seat_goes_below_each_warm_seat(self):
        result = plan([seat("full", 0.1, 5, five_hour_used=1.04), seat("warm", 0.9, 160, five_hour_used=0.9)])
        self.assertGreater(result["warm"], result["full"])

    def test_unavailable_seat_goes_below_each_open_seat(self):
        result = plan([seat("cool", 0.1, 5, unavailable=True), seat("open", 0.9, 160)])
        self.assertGreater(result["open"], result["cool"])

    def test_week_full_seat_is_last(self):
        result = plan([seat("week", 0.99, 5), seat("five", 0.1, 5, five_hour_used=1.0), seat("open", 0.5, 50)])
        self.assertLess(result["week"], result["five"])
        self.assertLess(result["five"], result["open"])

    def test_seat_with_no_signal_gets_the_top_priority(self):
        result = plan([seat("known", 0.0, 1), seat("unknown", None, None)])
        self.assertGreater(result["unknown"], result["known"])


class SignalTest(unittest.TestCase):
    def test_past_reset_means_an_empty_window(self):
        signals = {
            "Anthropic-Ratelimit-Unified-7d-Utilization": "0.8",
            "Anthropic-Ratelimit-Unified-7d-Reset": "1000",
        }
        self.assertEqual(window(signals, "7d", now=2000.0), (0.0, None))

    def test_cooldown_that_ended_does_not_block_the_seat(self):
        entry = {"name": "a.json", "unavailable": True, "next_retry_after": "2026-11-11T21:40:21.000937464+08:00"}
        self.assertFalse(read_seat(entry, now=4_000_000_000.0).unavailable)
        self.assertTrue(read_seat(entry, now=1_000_000_000.0).unavailable)

    def test_block_with_no_retry_time_stays(self):
        entry = {"name": "a.json", "unavailable": True}
        self.assertTrue(read_seat(entry, now=1_000_000_000.0).unavailable)


class GuardTest(unittest.TestCase):
    def test_no_guard_when_extra_usage_is_closed(self):
        full = replace(seat("a", 0.2, 50, five_hour_used=1.0), five_hour_reset_epoch=2000)
        self.assertIsNone(guard_until(full))

    def test_guard_blocks_until_the_five_hour_reset(self):
        full = replace(seat("a", 0.2, 50, five_hour_used=0.98), five_hour_reset_epoch=2000, overage_open=True)
        self.assertEqual(guard_until(full), 2000)

    def test_guard_prefers_the_weekly_reset(self):
        full = replace(
            seat("a", 0.99, 50, five_hour_used=0.99),
            five_hour_reset_epoch=2000, week_reset_epoch=9000, overage_open=True,
        )
        self.assertEqual(guard_until(full), 9000)

    def test_no_guard_below_the_limit(self):
        open_seat = replace(seat("a", 0.5, 50, five_hour_used=0.5), five_hour_reset_epoch=2000, overage_open=True)
        self.assertIsNone(guard_until(open_seat))

    def test_overage_signal_is_read(self):
        entry = {"name": "a.json", "quota": {"signals": {"Anthropic-Ratelimit-Unified-Overage-Status": "allowed"}}}
        self.assertTrue(read_seat(entry, now=1.0).overage_open)
        entry = {"name": "a.json", "quota": {"signals": {"Anthropic-Ratelimit-Unified-Overage-Status": "rejected"}}}
        self.assertFalse(read_seat(entry, now=1.0).overage_open)


if __name__ == "__main__":
    unittest.main()
