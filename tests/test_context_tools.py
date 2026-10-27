import random
import unittest

from wxbot_framework.conversation.context_tools import (
    ContextToolError, OFFENSIVE_WORDS, build_default_tools, is_offensive, pick_speech_examples,
    prefetch_mention_context, usable_example)


class FakeKnowledge:
    """记录每次调用的参数，按旧 mention_chat 的取值核对；数据全部虚构。"""

    def __init__(self):
        self.calls = []

    def member_entity(self, room, wxid):
        self.calls.append(("member_entity", room, wxid))
        return {"id": 7}

    def speaker_profile(self, room, wxid):
        self.calls.append(("speaker_profile", room, wxid))
        return {"bound": True, "name": "甲"}

    def search(self, room, query, *, entity_id=None, limit, char_limit):
        self.calls.append(("search", room, query, entity_id, limit, char_limit))
        if entity_id is not None:
            return [{"id": 1, "name": "own"}, {"id": 2, "name": "shared-own"}]
        return [{"id": 2, "name": "shared-rel"}, {"id": 3, "name": "rel"}]

    def lookup_people(self, room, query):
        self.calls.append(("lookup_people", room, query))
        return [{"name": "乙"}]

    def style(self, room, wxid):
        self.calls.append(("style", room, wxid))
        return "爱用省略号"


class FakeMembers:
    def __init__(self, rows):
        self.rows, self.options = rows, None

    def recent_rows(self, room, **options):
        self.options = options
        return self.rows


ROOM = "1@chatroom"


class RegistryTests(unittest.TestCase):
    def test_every_tool_has_name_description_and_parameters(self):
        tools = build_default_tools(FakeKnowledge())
        self.assertEqual(tools.names(), ("member_entity", "speaker_profile", "member_memory", "search_memory",
                                         "lookup_people", "speaker_style", "speech_examples"))
        for schema in tools.schemas():
            with self.subTest(tool=schema["name"]):
                self.assertTrue(schema["description"].strip())
                self.assertEqual(schema["parameters"]["type"], "object")
                self.assertEqual(set(schema["parameters"]["required"]), set(schema["parameters"]["properties"]))

    def test_unknown_tool_and_bad_arguments_are_rejected(self):
        tools = build_default_tools(FakeKnowledge())
        with self.assertRaises(ContextToolError):
            tools.call("no_such_tool")
        with self.assertRaises(ContextToolError):
            tools.call("lookup_people", room=ROOM)
        with self.assertRaises(ContextToolError):
            tools.call("lookup_people", room=ROOM, query="x", extra=1)

    def test_schema_copy_cannot_mutate_registry(self):
        tools = build_default_tools(FakeKnowledge())
        tools.schemas()[0]["parameters"]["required"].append("x")
        self.assertNotIn("x", tools.schemas()[0]["parameters"]["required"])


class PrefetchEqualsOldBehaviorTests(unittest.TestCase):
    def test_call_order_limits_and_dedupe_match_previous_inline_code(self):
        knowledge = FakeKnowledge()
        context = prefetch_mention_context(build_default_tools(knowledge), ROOM, "wxid_a", "今天吃什么")
        self.assertEqual(knowledge.calls, [
            ("member_entity", ROOM, "wxid_a"), ("speaker_profile", ROOM, "wxid_a"),
            ("search", ROOM, "", 7, 5, 1800), ("search", ROOM, "今天吃什么", None, 4, 2000),
            ("lookup_people", ROOM, "今天吃什么"), ("style", ROOM, "wxid_a")])
        # 与旧实现 list({id: item}.values()) 相同：后写入的同 id 条目覆盖前者，位置保持首次出现的位置。
        self.assertEqual([(item["id"], item["name"]) for item in context.knowledge],
                         [(1, "own"), (2, "shared-rel"), (3, "rel")])
        self.assertEqual(context.style, "爱用省略号")
        self.assertEqual(context.speaker_profile, {"bound": True, "name": "甲"})
        self.assertEqual(context.people, [{"name": "乙"}])

    def test_short_self_query_skips_relevant_search(self):
        knowledge = FakeKnowledge()
        context = prefetch_mention_context(build_default_tools(knowledge), ROOM, "wxid_a", "我是谁")
        searches = [call for call in knowledge.calls if call[0] == "search"]
        self.assertEqual(searches, [("search", ROOM, "", 7, 5, 1800)])
        self.assertEqual([item["id"] for item in context.knowledge], [1, 2])

    def test_long_query_with_self_hint_still_searches(self):
        knowledge = FakeKnowledge()
        query = "你知道我是谁吗还有我叫什么名字"
        self.assertGreater(len(query), 12)
        prefetch_mention_context(build_default_tools(knowledge), ROOM, "wxid_a", query)
        self.assertEqual(len([call for call in knowledge.calls if call[0] == "search"]), 2)


class FewShotTests(unittest.TestCase):
    def test_offensive_lines_are_filtered(self):
        for text in ("你去死吧哈哈", "你妈今天来吗", "他爸又加班了", "傻逼玩意儿", "这狗东西真烦", "地狱笑话来一个"):
            with self.subTest(text=text):
                self.assertTrue(is_offensive(text))
                self.assertFalse(usable_example(text))
        self.assertTrue(len(OFFENSIVE_WORDS) > 20)

    def test_length_and_shape_limits(self):
        self.assertFalse(usable_example("好"))  # 少于 4 字
        self.assertFalse(usable_example("哈" * 21))
        self.assertFalse(usable_example("第一行\n第二行"))
        self.assertFalse(usable_example("@小明 你来吗"))
        self.assertFalse(usable_example("看这个 https://example.com"))
        self.assertFalse(usable_example("/清空 一下"))
        self.assertTrue(usable_example("晚上吃火锅吧"))

    def test_pick_uses_non_bot_rows_dedupes_and_limits_count(self):
        rows = [{"id": index, "text": f"虚构短句{index:02d}号", "bot_generated": 0, "is_self": 0}
                for index in range(30)]
        rows += [{"id": 100, "text": "机器人说的话啊", "bot_generated": 1, "is_self": 1},
                 {"id": 101, "text": "虚构短句00号", "bot_generated": 0, "is_self": 0},
                 {"id": 102, "text": "你去死吧哈哈", "bot_generated": 0, "is_self": 0}]
        members = FakeMembers(rows)
        picked = pick_speech_examples(members, ROOM, rng=random.Random(5))
        self.assertEqual(len(picked), 10)
        self.assertEqual(len(set(picked)), 10)
        self.assertNotIn("机器人说的话啊", picked)
        self.assertNotIn("你去死吧哈哈", picked)
        self.assertEqual(picked, sorted(picked))  # 保持原发言顺序（此处序号与文本同序）
        self.assertIs(members.options["include_bot"], False)

    def test_pick_returns_all_when_fewer_than_count(self):
        rows = [{"id": 1, "text": "虚构短句甲号", "bot_generated": 0, "is_self": 0}]
        self.assertEqual(pick_speech_examples(FakeMembers(rows), ROOM), ["虚构短句甲号"])

    def test_pick_survives_missing_members_or_errors(self):
        class Broken:
            def recent_rows(self, room, **options):
                raise RuntimeError("库不可用")

        self.assertEqual(pick_speech_examples(None, ROOM), [])
        self.assertEqual(pick_speech_examples(Broken(), ROOM), [])

    def test_registry_tool_returns_the_same_picks(self):
        rows = [{"id": 1, "text": "虚构短句甲号", "bot_generated": 0, "is_self": 0}]
        tools = build_default_tools(FakeKnowledge(), FakeMembers(rows))
        self.assertEqual(tools.call("speech_examples", room=ROOM), ["虚构短句甲号"])


if __name__ == "__main__":
    unittest.main()
