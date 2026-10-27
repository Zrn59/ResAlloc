import unittest
from datetime import datetime, timedelta, timezone

from resalloc.models import Limits
from resalloc.policy import AccountSnapshot, Action, decide, lease_bounds, time_to_limit


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def limits(**kwargs) -> Limits:
    base = dict(soft_buffer=timedelta(minutes=15), hard_buffer=timedelta(minutes=5))
    base.update(kwargs)
    return Limits(**base)


class PolicyTest(unittest.TestCase):
    def test_plenty_of_money_continues(self) -> None:
        snapshot = AccountSnapshot("runpod", headroom_usd=8, burn_usd_per_hour=0.5)
        decision = decide(snapshot, limits(), has_next=True, confirm_next=False)
        self.assertIs(decision.action, Action.CONTINUE)

    def test_soft_buffer_migrates_and_waits(self) -> None:
        # 0.2 USD at 1.2 USD/hour is 10 minutes: inside 15, outside 5.
        snapshot = AccountSnapshot("runpod", headroom_usd=0.2, burn_usd_per_hour=1.2)
        decision = decide(snapshot, limits(), has_next=True, confirm_next=False)
        self.assertIs(decision.action, Action.MIGRATE)
        self.assertTrue(decision.wait_for_ack)
        self.assertEqual(decision.reason, "budget_headroom")

    def test_soft_buffer_parks_when_the_next_step_needs_confirm(self) -> None:
        snapshot = AccountSnapshot("runpod", headroom_usd=0.2, burn_usd_per_hour=1.2)
        decision = decide(snapshot, limits(), has_next=True, confirm_next=True)
        self.assertIs(decision.action, Action.PARK)

    def test_hard_buffer_does_not_wait(self) -> None:
        snapshot = AccountSnapshot("runpod", headroom_usd=0, burn_usd_per_hour=1)
        decision = decide(snapshot, limits(), has_next=True, confirm_next=False)
        self.assertIs(decision.action, Action.MIGRATE)
        self.assertFalse(decision.wait_for_ack)

    def test_no_failover_stops(self) -> None:
        snapshot = AccountSnapshot("modal", headroom_usd=0, burn_usd_per_hour=1)
        decision = decide(snapshot, limits(), has_next=False, confirm_next=False)
        self.assertIs(decision.action, Action.STOP)

    def test_hourly_cap(self) -> None:
        snapshot = AccountSnapshot("runpod", hourly_price_usd=1.2, headroom_usd=50, burn_usd_per_hour=1.2)
        decision = decide(
            snapshot,
            limits(runpod_max_hourly_usd=0.5),
            has_next=True,
            confirm_next=False,
        )
        self.assertEqual(decision.reason, "runpod_hourly_price")
        self.assertFalse(decision.wait_for_ack)

    def test_kaggle_quota(self) -> None:
        snapshot = AccountSnapshot("kaggle", quota_hours_left=0.1)
        remaining, reason = time_to_limit(snapshot, limits())
        self.assertEqual(reason, "accelerator_quota")
        assert remaining is not None
        self.assertLess(remaining, timedelta(minutes=15))

    def test_a_failed_script_does_not_migrate(self) -> None:
        snapshot = AccountSnapshot("runpod", alive=False, detail="worker_failed")
        decision = decide(snapshot, limits(), has_next=True, confirm_next=False)
        self.assertIs(decision.action, Action.STOP)
        self.assertEqual(decision.reason, "worker_failed")
        self.assertFalse(decision.wait_for_ack)

    def test_completed_does_not_migrate(self) -> None:
        snapshot = AccountSnapshot("laptop", alive=False, detail="completed")
        decision = decide(snapshot, limits(), has_next=True, confirm_next=False)
        self.assertIs(decision.action, Action.STOP)
        self.assertEqual(decision.reason, "completed")

    def test_dead_workload_does_not_wait(self) -> None:
        snapshot = AccountSnapshot("runpod", alive=False, detail="workload_stopped")
        decision = decide(snapshot, limits(), has_next=True, confirm_next=True)
        self.assertIs(decision.action, Action.PARK)
        self.assertFalse(decision.wait_for_ack)

    def test_lease_sits_one_buffer_before_the_limit(self) -> None:
        snapshot = AccountSnapshot("kaggle", quota_hours_left=2)
        soft, hard = lease_bounds(snapshot, limits(), NOW)
        self.assertEqual(soft, NOW + timedelta(hours=2) - timedelta(minutes=15))
        self.assertEqual(hard, NOW + timedelta(hours=2) - timedelta(minutes=5))

    def test_hard_buffer_cannot_exceed_soft(self) -> None:
        with self.assertRaises(ValueError):
            Limits(soft_buffer=timedelta(minutes=1), hard_buffer=timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
