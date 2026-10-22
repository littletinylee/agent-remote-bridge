"""Store-level guarantees: identity convergence, unique codes, atomic answers."""

from __future__ import annotations

import threading
import time
import unittest

from helpers import ArbTestCase

from arb.store import Store


class TestSessions(ArbTestCase):
    def test_same_key_reuses_row(self) -> None:
        s = Store(self.cfg.db_path)
        a = s.upsert_session(agent="claude-code", project="p", cwd="/x", key="sess-1")
        b = s.upsert_session(agent="claude-code", project="p", cwd="/x", key="sess-1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(s.q1("SELECT COUNT(*) c FROM sessions")["c"], 1)

    def test_distinct_keys_distinct_rows(self) -> None:
        s = Store(self.cfg.db_path)
        a = s.upsert_session(agent="claude-code", project="p", cwd="/x", key="sess-1")
        b = s.upsert_session(agent="claude-code", project="p", cwd="/x", key="sess-2")
        self.assertNotEqual(a["id"], b["id"])

    def test_cwd_fallback_converges(self) -> None:
        s = Store(self.cfg.db_path)
        a = s.upsert_session(agent="codex", project="p", cwd="D:/proj")
        b = s.upsert_session(agent="codex", project="p", cwd="D:/proj")
        self.assertEqual(a["id"], b["id"])


class TestQuestions(ArbTestCase):
    def test_codes_unique_while_pending(self) -> None:
        s = Store(self.cfg.db_path)
        sess = s.upsert_session(agent="a", project="p", cwd="/x")
        codes = set()
        for _ in range(60):
            q = s.create_question(session_id=sess["id"], prompt="q", options=["yes", "no"])
            self.assertNotIn(q.code, codes)
            codes.add(q.code)
            s.set_question_state(q.id, "answered")

    def test_claim_is_atomic_under_concurrency(self) -> None:
        s = Store(self.cfg.db_path)
        sess = s.upsert_session(agent="a", project="p", cwd="/x")
        q = s.create_question(session_id=sess["id"], prompt="q", options=["yes", "no"])
        wins: list[bool] = []
        barrier = threading.Barrier(8)

        def worker(i: int) -> None:
            store = Store(self.cfg.db_path)
            barrier.wait()
            won, _ = store.claim_answer(q.id, "yes" if i % 2 == 0 else "no", "raw", f"w{i}")
            wins.append(won)
            store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for w in wins if w), 1, "exactly one claimant must win")

    def test_cannot_answer_after_expiry(self) -> None:
        s = Store(self.cfg.db_path)
        sess = s.upsert_session(agent="a", project="p", cwd="/x")
        q = s.create_question(session_id=sess["id"], prompt="q", options=["yes", "no"])
        s.x("UPDATE questions SET expires_at=? WHERE id=?", (time.time() - 1, q.id))
        won, fresh = s.claim_answer(q.id, "yes", "yes", "owner")
        self.assertFalse(won)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.state, "pending")  # still pending until swept

    def test_expire_due_marks_once(self) -> None:
        s = Store(self.cfg.db_path)
        sess = s.upsert_session(agent="a", project="p", cwd="/x")
        q = s.create_question(session_id=sess["id"], prompt="q")
        s.x("UPDATE questions SET expires_at=? WHERE id=?", (time.time() - 1, q.id))
        first = s.expire_due()
        second = s.expire_due()
        self.assertEqual([x.id for x in first], [q.id])
        self.assertEqual(second, [])


class TestTextSafety(ArbTestCase):
    def test_lone_surrogates_are_replaced_not_fatal(self) -> None:
        s = Store(self.cfg.db_path)
        sess = s.upsert_session(agent="a", project="p\xdca3", cwd="/x")
        # would previously raise UnicodeEncodeError -> half-applied request
        q = s.create_question(session_id=sess["id"], prompt="bad \udca3 prompt", options=["a\u0301", "b"])
        s.enqueue("push \udca3 text")
        s.audit("test", "actor\udca3", {"x": "y\udca3"})
        self.assertIsNotNone(q)
        self.assertIn("bad", s.get_question(q.id).prompt)
        row = s.q("SELECT text FROM outbox ORDER BY id DESC LIMIT 1")[0]
        self.assertIn("push", row["text"])

    def test_inbound_with_surrogates_is_stored(self) -> None:
        s = Store(self.cfg.db_path)
        self.assertTrue(s.record_inbound("m-surrogate", "u", "hi \udca3"))

    def test_clean_text_is_idempotent(self) -> None:
        from arb.util import clean_text

        self.assertEqual(clean_text("正常文本"), "正常文本")
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(123), "123")
        self.assertNotIn("\udca3", clean_text("x\udca3y"))


class TestOutbox(ArbTestCase):
    def test_dedupe_and_retry(self) -> None:
        s = Store(self.cfg.db_path)
        a = s.enqueue("hello", dedupe="d1")
        b = s.enqueue("hello", dedupe="d1")
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        rows = s.due_outbox()
        self.assertEqual(len(rows), 1)
        s.mark_outbox_failed(rows[0]["id"], "boom", retry_in_s=0)
        self.assertEqual(s.outbox_stats()["failing"], 0)
        s.mark_outbox_failed(rows[0]["id"], "boom2", retry_in_s=0)
        s.mark_outbox_failed(rows[0]["id"], "boom3", retry_in_s=0)
        self.assertEqual(s.outbox_stats()["failing"], 1)

    def test_inbound_dedupe(self) -> None:
        s = Store(self.cfg.db_path)
        self.assertTrue(s.record_inbound("m1", "u", "hi"))
        self.assertFalse(s.record_inbound("m1", "u", "hi"))


if __name__ == "__main__":
    unittest.main()
