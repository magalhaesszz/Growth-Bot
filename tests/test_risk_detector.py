import asyncio
import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from instagram.risk_detector import LOCAL_TZ, RiskDetector


class RiskDetectorTests(unittest.TestCase):
    def test_session_expired_notifies_only_once_until_resume(self):
        detector = RiskDetector()
        messages = []

        async def notify(message):
            messages.append(message)

        detector.set_notify_fn(notify)
        self.assertTrue(detector.notify_session_expired("conta"))
        self.assertFalse(detector.notify_session_expired("conta"))
        self.assertEqual(len(messages), 1)

        detector.resume("conta")
        self.assertTrue(detector.notify_session_expired("conta"))
        self.assertEqual(len(messages), 2)

    def test_persisted_pause_survives_new_detector_instance(self):
        store = {}

        def load(username):
            return store.get(username, {})

        def save(username, data):
            store[username] = dict(data)
            return True

        first = RiskDetector()
        first.set_persistence(load, save)
        first.pause("conta", "challenge")
        self.assertTrue(store["conta"]["is_paused"])

        second = RiskDetector()
        second.set_persistence(load, save)
        status = second.get_status("conta")
        self.assertTrue(status["is_paused"])
        self.assertEqual(status["pause_reason"], "challenge")

        second.resume("conta")
        third = RiskDetector()
        third.set_persistence(load, save)
        self.assertFalse(third.is_paused("conta"))

    def test_anomaly_alert_is_deduplicated(self):
        detector = RiskDetector()
        state = detector._state("conta")
        state.last_action_at = (
            datetime.now(LOCAL_TZ).replace(tzinfo=None) - timedelta(hours=3)
        )

        self.assertTrue(detector.check_anomaly("conta", 0, 24))
        self.assertFalse(detector.check_anomaly("conta", 0, 24))

    def test_paused_account_does_not_emit_anomaly(self):
        detector = RiskDetector()
        state = detector._state("conta")
        state.last_action_at = (
            datetime.now(LOCAL_TZ).replace(tzinfo=None) - timedelta(hours=3)
        )
        detector.pause("conta", "risco")
        self.assertFalse(detector.check_anomaly("conta", 0, 24))


class RiskDetectorAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_thread_notification_reaches_main_loop_once(self):
        detector = RiskDetector()
        event = asyncio.Event()
        messages = []

        async def notify(message):
            messages.append(message)
            event.set()

        detector.set_notify_fn(notify, loop=asyncio.get_running_loop())
        created = await asyncio.to_thread(detector.notify_session_expired, "conta")
        self.assertTrue(created)
        await asyncio.wait_for(event.wait(), timeout=1)
        self.assertEqual(len(messages), 1)

        created_again = await asyncio.to_thread(
            detector.notify_session_expired, "conta"
        )
        self.assertFalse(created_again)
        await asyncio.sleep(0.05)
        self.assertEqual(len(messages), 1)


if __name__ == "__main__":
    unittest.main()
