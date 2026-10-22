"""Quoted replies, against a real captured WeChat payload."""

from __future__ import annotations

import unittest

from arb.channel.ilink import IlinkChannel

REAL_QUOTED = {
    "seq": 9,
    "message_id": 7518929780184907272,
    "from_user_id": "o9cq8050GDr6ElW4DLz66D7_-a9c@im.wechat",
    "to_user_id": "011c58aef33e@im.bot",
    "message_type": 1,
    "context_token": "AARzJWAFAAABAAAAAAAtYclmbP",
    "item_list": [{
        "type": 1,
        "msg_id": "v1:10405711719182481819",
        "ref_msg": {"message_item": {"type": 0, "msg_id": "7518928040791637512"}},
        "text_item": {"text": "1"},
    }],
}


class TestQuotedParse(unittest.TestCase):
    def test_nested_ref_is_found(self) -> None:
        parsed = IlinkChannel._parse(REAL_QUOTED)
        assert parsed is not None
        self.assertTrue(parsed.quoted)
        self.assertEqual(parsed.ref_svr_id, "7518928040791637512")
        self.assertEqual(parsed.text, "1")

    def test_plain_message_has_no_reference(self) -> None:
        plain = {
            "message_id": 1,
            "from_user_id": "o",
            "message_type": 1,
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }
        parsed = IlinkChannel._parse(plain)
        assert parsed is not None
        self.assertFalse(parsed.quoted)
        self.assertEqual(parsed.ref_svr_id, "")

    def test_quoted_text_with_a_header_yields_the_code(self) -> None:
        quoted = {
            "message_id": 2,
            "from_user_id": "o",
            "message_type": 1,
            "item_list": [{
                "type": 1,
                "text_item": {"text": "yes"},
                "ref_msg": {"message_item": {"msg_id": "9", "text_item": {"text": "🚨 需要你决策  #AB34\n[p]\nq"}}},
            }],
        }
        parsed = IlinkChannel._parse(quoted)
        assert parsed is not None
        self.assertEqual(parsed.ref_code, "AB34")


if __name__ == "__main__":
    unittest.main()
