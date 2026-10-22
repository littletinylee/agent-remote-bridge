"""Channel supervisor: a channel that keeps dying must be restarted, and a
never-ending failure must stay visible instead of silently going quiet.
"""

from __future__ import annotations

import threading
import time
import unittest

from helpers import ArbTestCase

from arb.channel.console import MemoryChannel
from arb.daemon import Daemon
from arb.store import Store


class FlakyChannel(MemoryChannel):
    """Returns from start() immediately, like a channel whose token expired."""

    name = "flaky"

    def __init__(self, fail_times: int = 3) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.calls = 0
        self.auth_error = "微信登录已失效（errcode -14）"

    def start(self, on_message, stop_event) -> None:  # type: ignore[override]
        self.calls += 1
        if self.calls >= self.fail_times:
            stop_event.set()  # end the test
        return


class TestChannelSupervisor(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.channel_retry_s = 0.05
        self.cfg.save()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def test_channel_is_restarted_and_reported(self) -> None:
        channel = FlakyChannel(fail_times=3)
        daemon = Daemon(self.cfg, channel=channel, store=self.store)
        daemon.start()
        self.assertTrue(self.wait_until(lambda: channel.calls >= 3, timeout=5), channel.calls)
        daemon.shutdown()

        audits = {r["action"] for r in self.store.audit_tail(50)}
        self.assertIn("channel_restart_pending", audits)
        self.assertGreaterEqual(channel.calls, 3, "the supervisor must retry, not give up")

    def test_status_reports_the_failure(self) -> None:
        channel = FlakyChannel(fail_times=2)
        daemon = Daemon(self.cfg, channel=channel, store=self.store)
        daemon.start()
        self.wait_until(lambda: channel.calls >= 1, timeout=5)
        time.sleep(0.2)
        payload = daemon.status_payload()
        daemon.shutdown()
        self.assertFalse(payload["channel"]["healthy"])
        self.assertIn(payload["channel"]["state"], {"down", "starting"})
        self.assertTrue(payload["channel"]["auth_error"] or payload["channel"]["last_error"])


if __name__ == "__main__":
    unittest.main()
