"""Tests for the priority plan of the reset-aware router."""

import unittest

from dataclasses import replace

from router import Seat, guard_until, parse_weights, plan, read_seat, recall, snapshot, state_of, window


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


class WeightTest(unittest.TestCase):
    def test_weight_multiplies_the_priority(self):
        result = plan([seat("main", 0.5, 50), replace(seat("half", 0.5, 50), weight=0.5)])
        self.assertEqual(result["main"], 100)
        self.assertEqual(result["half"], 50)

    def test_weighted_seats_rank_apart_from_the_other_seats(self):
        spare = replace(seat("spare", None, None), weight=0.5)
        result = plan([spare, seat("main", 0.9, 160)])
        self.assertEqual(result["main"], 100)
        self.assertEqual(result["spare"], 50)

    def test_smaller_weight_goes_below_larger_weight(self):
        half = replace(seat("half", 0.0, 1), weight=0.5)
        quarter = replace(seat("quarter", 0.0, 1), weight=0.25)
        result = plan([quarter, half, seat("main", 0.9, 160)])
        self.assertGreater(result["main"], result["half"])
        self.assertGreater(result["half"], result["quarter"])

    def test_weighted_seat_with_quota_stays_above_each_full_seat(self):
        last = replace(seat("last", 0.5, 50), weight=0.01)
        result = plan([last, seat("five", 0.1, 5, five_hour_used=1.0), seat("week", 0.99, 5)])
        self.assertGreater(result["last"], result["five"])
        self.assertGreater(result["last"], result["week"])

    def test_full_weighted_seat_ranks_as_a_full_seat(self):
        spare = replace(seat("spare", 0.99, 5), weight=0.5)
        result = plan([spare, seat("five", 0.1, 5, five_hour_used=1.0)])
        self.assertLess(result["spare"], result["five"])

    def test_no_weight_keeps_the_priorities(self):
        seats = [seat("a", 0.2, 10), seat("b", 0.2, 120), seat("c", 0.1, 5, five_hour_used=0.9)]
        weighted = [replace(item, weight=1.0) for item in seats]
        self.assertEqual(plan(seats), plan(weighted))

    def test_weights_are_read_per_pool(self):
        weights, errors = parse_weights("work=spare:0.5,extra:0.25 other=one:2")
        self.assertEqual(weights, {"work": {"spare": 0.5, "extra": 0.25}, "other": {"one": 2.0}})
        self.assertEqual(errors, [])
        self.assertEqual(parse_weights(""), ({}, []))

    def test_bad_weight_items_are_reported_and_ignored(self):
        weights, errors = parse_weights("work=ok:0.5,zero:0,word:x,noweight bad =x:1")
        self.assertEqual(weights, {"work": {"ok": 0.5}})
        self.assertEqual(len(errors), 5)


class ProviderTest(unittest.TestCase):
    def test_seats_of_two_types_rank_apart(self):
        codex = replace(seat("codex", 0.9, 160), kind="codex")
        result = plan([seat("claude", 0.1, 10), codex])
        self.assertEqual(result["claude"], 100)
        self.assertEqual(result["codex"], 100)

    def test_codex_windows_are_read_by_their_length(self):
        entry = {"name": "c.json", "type": "codex", "quota": {"signals": {
            "X-Codex-Primary-Used-Percent": "51",
            "X-Codex-Primary-Window-Minutes": "10080",
            "X-Codex-Primary-Reset-At": "8200",
            "X-Codex-Secondary-Used-Percent": "20",
            "X-Codex-Secondary-Window-Minutes": "300",
            "X-Codex-Secondary-Reset-At": "4600",
        }}}
        result = read_seat(entry, now=1000.0)
        self.assertEqual((result.week_used, result.week_hours_left, result.week_reset_epoch), (0.51, 2.0, 8200))
        self.assertEqual((result.five_hour_used, result.five_hour_hours_left), (0.2, 1.0))
        self.assertFalse(result.overage_open)

    def test_codex_slot_names_decide_when_no_length_is_given(self):
        entry = {"name": "c.json", "type": "codex", "quota": {
            "observed_at": "2026-01-01T00:00:00+00:00",
            "signals": {
                "x-codex-primary-used-percent": "99",
                "x-codex-primary-reset-after-seconds": "3600",
                "x-codex-secondary-used-percent": "10",
            },
        }}
        observed = 1767225600.0
        result = read_seat(entry, now=observed + 1800)
        self.assertEqual((result.five_hour_used, result.five_hour_hours_left), (0.99, 0.5))
        self.assertEqual(result.week_used, 0.1)
        self.assertEqual(state_of(result), "five_hour_full")

    def test_codex_window_that_ended_is_empty(self):
        entry = {"name": "c.json", "type": "codex", "quota": {"signals": {
            "X-Codex-Primary-Used-Percent": "100", "X-Codex-Primary-Window-Minutes": "300",
            "X-Codex-Primary-Reset-At": "500",
        }}}
        self.assertEqual(read_seat(entry, now=1000.0).five_hour_used, 0.0)

    def test_seat_of_an_unknown_type_has_no_quota_view(self):
        entry = {"name": "g.json", "type": "other", "quota": {"signals": {
            "Anthropic-Ratelimit-Unified-7d-Utilization": "0.99",
        }}}
        result = read_seat(entry, now=1.0)
        self.assertIsNone(result.week_used)
        self.assertEqual(state_of(result), "open")

    def test_claude_signal_names_match_in_any_case(self):
        entry = {"name": "a.json", "type": "claude", "quota": {"signals": {
            "anthropic-ratelimit-unified-7d-utilization": "0.5",
            "ANTHROPIC-RATELIMIT-UNIFIED-7D-RESET": "4600",
        }}}
        result = read_seat(entry, now=1000.0)
        self.assertEqual((result.week_used, result.week_hours_left, result.week_reset_epoch), (0.5, 1.0, 4600))


class MemoryTest(unittest.TestCase):
    def full(self):
        return replace(seat("a", 1.0, 50, five_hour_used=0.4), week_reset_epoch=9000, five_hour_reset_epoch=3000)

    def test_seat_with_no_quota_view_gets_the_remembered_quota(self):
        result = recall(seat("a", None, None, five_hour_used=None), snapshot(self.full()), now=2000.0)
        self.assertEqual(state_of(result), "week_full")
        self.assertEqual((result.week_used, result.week_reset_epoch), (1.0, 9000))
        self.assertEqual(result.five_hour_used, 0.4)

    def test_remembered_window_that_ended_is_not_used(self):
        result = recall(seat("a", None, None, five_hour_used=None), snapshot(self.full()), now=10000.0)
        self.assertIsNone(result.week_used)
        self.assertIsNone(result.five_hour_used)
        self.assertEqual(plan([result, seat("b", 0.5, 50)])["a"], 100)

    def test_quota_from_the_proxy_wins_over_the_memory(self):
        fresh = seat("a", 0.1, 160)
        self.assertEqual(recall(fresh, snapshot(self.full()), now=2000.0), fresh)

    def test_no_record_keeps_the_seat(self):
        unknown = seat("a", None, None, five_hour_used=None)
        self.assertEqual(recall(unknown, None, now=2000.0), unknown)

    def test_remembered_full_seat_goes_below_an_open_seat(self):
        full = recall(seat("full", None, None, five_hour_used=None), snapshot(self.full()), now=2000.0)
        result = plan([full, seat("open", 0.0, 160)])
        self.assertGreater(result["open"], result["full"])


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
        entry = {"name": "a.json", "type": "claude", "quota": {"signals": {"Anthropic-Ratelimit-Unified-Overage-Status": "allowed"}}}
        self.assertTrue(read_seat(entry, now=1.0).overage_open)
        entry = {"name": "a.json", "type": "claude", "quota": {"signals": {"Anthropic-Ratelimit-Unified-Overage-Status": "rejected"}}}
        self.assertFalse(read_seat(entry, now=1.0).overage_open)


if __name__ == "__main__":
    unittest.main()
