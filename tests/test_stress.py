"""Scale / concurrency stress: many sessions, many questions, shuffled answers.

The unit tests prove the rules for one question; this proves they still hold
when 20 questions from 10 sessions are live at once and replies arrive out of
order, and that a duplicated approval can never win twice.
"""

from __future__ import annotations

import random
import threading
import unittest

from helpers import ArbTestCase

from arb.channel.base import InboundMessage
from arb.channel.console import MemoryChannel
from arb.daemon import Daemon, chunk_text
from arb.store import Store


class TestManyConcurrentQuestions(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.owner_ids = ["owner"]
        self.cfg.notify_min_interval_s = 0
        self.cfg.save()
        self.store = Store(self.cfg.db_path)
        self.channel = MemoryChannel()
        self.daemon = Daemon(self.cfg, channel=self.channel, store=self.store)
        self.daemon.start()

    def tearDown(self) -> None:
        self.daemon.shutdown()
        self.store.close()
        super().tearDown()

    def test_twenty_questions_ten_sessions_shuffled(self) -> None:
        sessions = [
            self.daemon.resolve_session({"agent": f"agent{i % 3}", "project": f"proj{i}", "cwd": f"D:/p{i}", "key": f"k{i}"})
            for i in range(10)
        ]
        asked: list[tuple[str, object]] = []
        for i, sess in enumerate(sessions):
            for j in range(2):
                q = self.daemon.ask(
                    session_id=sess["id"],
                    prompt=f"proj{i} #q{j}",
                    options=[f"opt-{i}-{j}-A", f"opt-{i}-{j}-B"],
                )
                asked.append((sess["id"], q))
        self.assertTrue(self.channel.wait_sent(len(asked), timeout=15), "every question must be pushed")

        # every push must be tagged with its own project and code
        text = "\n".join(m["text"] for m in self.channel.sent)
        for _sid, q in asked:
            self.assertIn(q.code, text)

        shuffled = asked[:]
        random.shuffle(shuffled)
        for _sid, q in shuffled:
            self.channel.push(f"#{q.code} 2", from_user="owner")

        for sid, q in asked:
            final = self.daemon.wait_for(q.id, timeout=10)
            self.assertEqual(final.state, "answered", f"{q.code} should be answered")
            self.assertEqual(final.session_id, sid, "answer must stay on its own session")
            self.assertTrue(final.answer.endswith("-B"), f"{q.code} got {final.answer}")

        answered = self.store.q("SELECT COUNT(*) c FROM questions WHERE state='answered'")[0]["c"]
        self.assertEqual(answered, len(asked))

    def test_duplicate_approvals_for_one_question_only_one_wins(self) -> None:
        sess = self.daemon.resolve_session({"agent": "claude-code", "project": "p", "cwd": "D:/p", "key": "dup"})
        q = self.daemon.ask(session_id=sess["id"], prompt="危险", options=["allow", "deny"], dangerous=True)
        self.assertTrue(self.channel.wait_sent(1, timeout=5))

        outcomes: list[str] = []
        lock = threading.Lock()

        def reply(i: int) -> None:
            msg = InboundMessage(msg_id=f"m-{i}", from_user="owner", text=f"/approve {q.code}")
            out = self.daemon.handle_message(msg)
            with lock:
                outcomes.append(out)

        threads = [threading.Thread(target=reply, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sum(1 for o in outcomes if o.startswith("answered")), 1, outcomes)
        self.assertEqual(self.store.get_question(q.id).answer, "allow")


class TestChunking(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        self.assertEqual(chunk_text("hello", 100), ["hello"])

    def test_chunks_respect_limit_and_preserve_content(self) -> None:
        text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(200))
        chunks = chunk_text(text, 500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 500)
        joined = "".join(chunks)
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))

    def test_pathological_single_line(self) -> None:
        chunks = chunk_text("y" * 1200, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(chunks), "y" * 1200)


if __name__ == "__main__":
    unittest.main()
