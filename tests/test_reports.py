"""The committed verification artifacts must not leak identifiers."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("report_util", ROOT / "scripts" / "report_util.py")
report_util = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(report_util)


class TestScrub(unittest.TestCase):
    def test_wechat_ids_are_masked(self) -> None:
        out = report_util.scrub({"answer_by": "o9cq8050GDr6ElW4DLz66D7_-a9c@im.wechat"})
        self.assertEqual(out["answer_by"], "o9cq…@im.wechat")
        out = report_util.scrub("bot ffbbf02dedb3@im.bot")
        self.assertNotIn("ffbbf02dedb3", out)

    def test_bot_token_secret_is_masked(self) -> None:
        out = report_util.scrub("ffbbf02dedb3@im.bot:0600000ad1e7f0c3a4d315eb8afb7371bed2aa")
        self.assertNotIn("0600000ad1e7f0c3", out)

    def test_api_keys_are_masked(self) -> None:
        self.assertNotIn("abcdefgh", report_util.scrub("sk-abcdefghijklmnop"))
        self.assertNotIn("abcdefgh", report_util.scrub("github_pat_abcdefghijkl"))

    def test_windows_paths_lose_the_username(self) -> None:
        out = report_util.scrub_str(r"D:\proj and C:\Users\alice\AppData\log.txt")
        self.assertNotIn("alice", out)
        self.assertIn("<user>", out)

    def test_nested_structures(self) -> None:
        data = {"a": [{"b": "x@im.wechat-not-an-id"}], "c": 3}
        self.assertEqual(report_util.scrub(data)["c"], 3)


class TestCommittedArtifacts(unittest.TestCase):
    def test_every_report_is_already_clean(self) -> None:
        for path in sorted((ROOT / "docs" / "verification").glob("*.json")):
            original = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report_util.scrub(original), original, f"{path.name} still leaks identifiers")


if __name__ == "__main__":
    unittest.main()
