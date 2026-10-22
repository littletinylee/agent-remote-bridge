"""Shared test fixtures: isolated ARB_HOME per test, temp DBs, helpers."""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ArbTestCase(unittest.TestCase):
    """Gives every test a fresh ARB_HOME so nothing leaks between tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="arb-test-")
        self._old_home = os.environ.get("ARB_HOME")
        os.environ["ARB_HOME"] = self._tmp
        from arb.config import Config

        # bootstrap() is what `arb init` does: fill in the API token/pairing code
        # so tests exercise the same on-disk state production has.
        self.cfg = Config().bootstrap()
        self.cfg.port = 0  # bind an ephemeral port; tests read the real one back
        self.cfg.save()
        self.home = Path(self._tmp)

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("ARB_HOME", None)
        else:
            os.environ["ARB_HOME"] = self._old_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def wait_until(self, predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False
