"""从群消息到查证、再到次轮命中和发送的纠错回放。"""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import test_image_generation as fixture
from plugins.mention_chat import reply_seconds
from plugins.mention_chat.image_intent import FORMAT_PROMPT
from wxbot_framework.app.maintenance import MaintenanceService
from wxbot_framework.authorization import LOCAL_EDITOR
from wxbot_framework.conversation.correction_capture import PROPOSE
from wxbot_framework.conversation.correction_lookup import candidates, route
from wxbot_framework.conversation.mention_tools import MentionTools, ToolResult, read_tool_options
from wxbot_framework.conversation.turns import CORRECTION_INTENT
from wxbot_framework.integrations.magpie_tools import MagpieResult


ROOM = fixture.ROOM
URL = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/nvda-20261111.htm"
SOURCE = "On November 11, 2026, the Board accepted Jensen Huang's resignation as CEO, effective immediately."
CLAIM = "黄仁勋于2026年11月11日辞去英伟达CEO，当天生效。"
PYTHON_URL = "https://docs.python.org/3.14/whatsnew/3.14.html"
PYTHON_SOURCE = "Python 3.14: Free-threaded Python is officially supported and remains optional."
PROPOSAL = {"范围": "公开", "对象": "黄仁勋", "关系": "辞任CEO", "事实": CLAIM, "类别": "事件",
            "生效日期": "2026-11-11", "资料确认至": "2026-11-12", "关联名称": ["英伟达", "Jensen Huang", "老黄"],
            "常见问法": ["黄仁勋辞职了吗", "英伟达的老黄是不是早退了"], "网址": URL, "依据类型": "公开"}


class Judge:
    def __init__(self):
        self.lookups = []
        self.reviewed = []

    def noul(self, state, questions, **options):
        if "这轮要回答的问题" in state:
            correction = "你搞错了" in state["这轮要回答的问题"]
            return [0.95, 0.02, 0.95] if correction else [0.02, 0.02, 0.02]
        if "拟记录事实" in state:
            evidence = state["证据"]
            if evidence["类型"] == "群决定":
                return [0.96 if "确认是本群管理员" in evidence["发言身份"] and "北门" in evidence["正文"] else 0.03]
            return [0.96 if evidence["正文"] in (SOURCE, PYTHON_SOURCE,
                                                "@机器人\u2005你记错了，我本人改玩羽毛球了。") else 0.03]
        if "当前发言" in state and any(word in state["当前发言"] for word in ("我本人", "旧方案")):
            return [0.96 if q == CORRECTION_INTENT else 0.02 for q in questions]
        if "本轮公开资料" in state:
            return [0.95 if CLAIM in state["拟回答"] else 0.02]
        if "候选" in state and "本句" in state:
            self.lookups.append(state)
            direct = any(word in state["本句"] for word in
                         ("辞职", "搞错了", "记错了", "谁接班", "碰头", "旧方案", "自由线程", "无 GIL"))
            current = "现在" in state["本句"]
            ambiguous = "他们" in state["本句"] and not state["明确引用及原问题"]
            return [float(ambiguous), float(current)] + [
                number for item in state["候选"]
                for number in (0.96, 0.96 if direct else 0.01, 0.92 if "去哪" in state["本句"] else 0.02)]
        if "拟发送的回答" in state:
            checked = state["依据"].get("已核实纠错", [])
            self.reviewed.append((state["拟发送的回答"], checked))
            return [0.02] * (len(questions) - 1) + [
                0.96 if checked and "仍是英伟达CEO" in state["拟发送的回答"] else 0.02]
        return [0.02] * len(questions)


class Model:
    def __init__(self, proposal=None):
        self.proposal = proposal or PROPOSAL
        self.prompts = []
        self.replies = []

    def complete_json(self, system, user, **options):
        data = json.loads(user)
        if system == FORMAT_PROMPT:
            return {"动作": "text"}
        if system == PROPOSE:
            return {"纠错": [self.proposal]}
        if not any(tool["name"] == "search_public" for tool in data["可用工具"]):
            return {"依据": []}
        if not data["取材记录"]:
            return fixture.lookup("search_public", query="英伟达 黄仁勋 董事会 辞任 CEO 8-K")
        source = data["取材记录"][0][0]["结果"]["资料"]["来源编号"]
        return {"依据": [source]}

    def complete_text(self, system, user, **options):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else CLAIM


class VerifiedCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ImageGenerationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.plugin, self.ctx = self.fixture.plugin, self.fixture.ctx
        self.members = self.fixture.members
        self.store = self.fixture.knowledge.corrections
        self.plugin.tools = read_tool_options({})
        self.judge, self.model = Judge(), Model()
        self.ctx._jev, self.ctx._llm = self.judge, self.model

    def send(self, question, *, sender="wxid_member", quote=None):
        data = {"is_at_self": True, "at_members": ["wxid_robot"]}
        if quote:
            data["quote"] = quote
        message = self.fixture.message("@机器人\u2005" + question, sender=sender, data=data)
        self.plugin.on_message(message, self.ctx)
        return message

    def seed(self, **changes):
        payload = dict(scope="公开", subject_key="黄仁勋", subject="黄仁勋", relation="辞任ceo", kind="事件",
                       statement=CLAIM, names=("黄仁勋", "英伟达", "老黄"), phrases=("黄仁勋辞职了吗",),
                       effective_on="2026-11-11", observed_on="2026-11-12",
                       source={"网址": URL, "正文": SOURCE})
        return self.store.record(ROOM, {**payload, **changes}, 1)

    def test_message_learns_from_fetched_primary_source_then_answers_another_member_after_clear(self):
        self.members.record_bot_reply(ROOM, "黄仁勋仍是英伟达CEO。")
        with (patch.object(MentionTools, "search", return_value=ToolResult("search", True, "SEC 8-K " + URL)) as search,
              patch.object(self.fixture.magpie, "fetch", return_value=MagpieResult(SOURCE)) as fetch):
            self.send("你搞错了，黄仁勋已经辞职了。")
        fetch.assert_called_once_with(URL)
        self.assertEqual(search.call_count, 1)
        first, = self.store.list(ROOM)
        self.assertEqual((first.statement, first.source["网址"]), (CLAIM, URL))
        self.members.reset_context(ROOM)
        self.model.replies = ["老黄仍是英伟达CEO。", "老黄仍是英伟达CEO。"]
        with patch.object(MentionTools, "search") as repeated_search:
            request = self.send("英伟达的老黄辞职了吗？", sender="wxid_next")
        repeated_search.assert_not_called()
        self.assertIn(CLAIM, self.fixture.backend.texts[-1][1])
        self.assertNotIn("仍是", self.fixture.backend.texts[-1][1])
        self.assertTrue(any("本轮命中的已核实纠错" in prompt and "直接答案" in prompt for prompt in self.model.prompts))
        self.assertEqual(self.store.used(ROOM, (self.members.message_id(request),)), {first.id})

    def test_search_summary_alone_does_not_create_a_fact(self):
        with (patch.object(MentionTools, "search", return_value=ToolResult("search", True, "SEC 8-K " + URL)),
              patch.object(self.fixture.magpie, "fetch", return_value=MagpieResult("只有一篇旧职位简介。"))):
            self.send("你搞错了，黄仁勋已经辞职了。")
        self.assertEqual(self.store.list(ROOM), [])

    def test_quote_without_a_name_can_recover_the_linked_question(self):
        self.seed(subject="英伟达", subject_key="英伟达", relation="临时ceo", kind="状态", names=("英伟达", "Colette Kress"),
                  phrases=("谁接班", "后来谁掌舵"), statement="2026年11月17日，Colette Kress仍是英伟达临时CEO。")
        anchor = self.fixture.message("英伟达高管离任后的安排有什么变化？")
        self.members.record_bot_reply(ROOM, "公司有临时安排。", source_id=self.members.message_id(anchor))
        self.fixture.message("待会儿点什么外卖？", sender="wxid_other")
        self.model.replies = ["Colette Kress当时担任临时CEO。"]
        self.send("那接他班的是谁？", sender="wxid_next",
                  quote={"ref_type": "1", "fromusr": "wxid_robot", "text": "公司有临时安排。"})
        self.assertIn("英伟达高管", self.judge.lookups[-1]["明确引用及原问题"])
        self.assertIn("Colette Kress", self.fixture.backend.texts[-1][1])

    def test_related_fact_used_as_premise_without_turning_into_answer(self):
        self.seed()
        self.fixture.message("英伟达的黄仁勋离开了CEO职位。", sender="wxid_other")
        self.model.replies = ["去向还没公布。"]
        with patch.object(MentionTools, "search") as search:
            self.send("那老黄去哪呢？")
        search.assert_not_called()
        self.assertIn("承接前提", self.model.prompts[-1])
        self.assertNotIn("直接答案", self.model.prompts[-1])
        self.assertIn("去向还没公布", self.fixture.backend.texts[-1][1])

    def test_facts_named_only_in_draft_are_also_checked(self):
        self.seed()
        self.model.replies = ["宏观局势波动很大，黄仁勋仍是英伟达CEO。", "宏观局势波动很大。"]
        self.send("最近市场气氛咋样？")
        self.assertFalse(self.judge.lookups)
        self.assertTrue(any("仍是" in text and rows for text, rows in self.judge.reviewed))
        self.assertNotIn("黄仁勋", self.fixture.backend.texts[-1][1])

    def test_bound_members_can_correct_their_own_preference(self):
        today = self.members.now().date().isoformat()
        self.members.register("阿青", ROOM, "wxid_qing")
        self.members.record_bot_reply(ROOM, "阿青现在还在打网球。")
        self.model.proposal = {"范围": "本群", "对象": "阿青", "关系": "运动偏好", "类别": "状态",
                               "事实": "阿青现在改玩羽毛球。", "消息编号": 2, "依据类型": "本人",
                               "生效日期": today, "资料确认至": today, "常见问法": ["阿青平常玩什么"]}
        self.model.replies = ["说错了，你现在改玩羽毛球。"]
        self.send("你记错了，我本人改玩羽毛球了。", sender="wxid_qing")
        saved, = self.store.list(ROOM)
        self.assertEqual(saved.subject_key, "member:wxid_qing")
        self.assertEqual(saved.source["消息编号"], 2)

    def test_active_and_historical_versions_and_revoked_entry(self):
        older = self.seed(subject="英伟达", subject_key="英伟达", relation="临时ceo", kind="状态",
                          effective_on="2026-11-12", observed_on="2026-11-17",
                          statement="11月12日开始由A暂代。")
        newer = self.seed(subject="英伟达", subject_key="英伟达", relation="临时ceo", kind="状态",
                          effective_on="2026-11-20", observed_on="2026-11-20",
                          statement="11月20日由B接任。")
        facts = self.store.list(ROOM)
        self.assertEqual(len(candidates(facts, "英伟达谁接班")), 1)
        kwargs = dict(now=datetime(2026, 11, 22, tzinfo=timezone.utc), logger=self.ctx.logger, anchor_id=1)
        historical = route(facts, "英伟达11月17日谁接班？", self.judge, **kwargs)
        present = route(facts, "英伟达现在谁接班？", self.judge, **kwargs)
        self.assertEqual([row.id for row in historical.answers], [older])
        self.assertEqual([row.id for row in present.answers], [newer])
        overdue = route(facts, "英伟达现在谁接班？", self.judge,
                        **{**kwargs, "now": datetime(2026, 11, 26, tzinfo=timezone.utc)})
        self.assertEqual([row.id for row in overdue.refresh], [newer])
        admin = MaintenanceService(self.fixture.knowledge)
        self.assertIn(URL, admin.execute(f"纠错 来源 {newer}", LOCAL_EDITOR, current_room=ROOM))
        admin.execute(f"纠错 停用 {newer}", LOCAL_EDITOR, current_room=ROOM)
        self.assertIsNone(self.seed(subject="英伟达", subject_key="英伟达", relation="临时ceo", kind="状态",
                                    effective_on="2026-11-20", observed_on="2026-11-22",
                                    statement="重新转述B接任。"))
        self.assertEqual([row.id for row in self.store.list(ROOM)], [older])
        self.assertEqual(self.store.list("another@chatroom"), [])

    def test_indexed_relation_recovers_a_direct_paraphrase_without_accepting_translation(self):
        number = self.seed(subject="英伟达", subject_key="英伟达", relation="临时ceo", kind="状态",
                           names=("英伟达",), phrases=("谁掌舵",), statement="Colette Kress当时暂代。")
        options = dict(now=datetime(2026, 11, 22, tzinfo=timezone.utc), logger=self.ctx.logger, anchor_id=1)
        facts = self.store.list(ROOM)
        with patch.object(self.judge, "noul", side_effect=[[0.29, 0.70, 0.88, 0.59, 0.13],
                                                          [0.02, 0.01, 0.88, 0.04, 0.01]]):
            found = route(facts, "英伟达后来谁掌舵了？", self.judge, **options)
            translated = route(facts, "把‘英伟达谁掌舵’这句翻译成英文", self.judge, **options)
        self.assertEqual([row.id for row in found.answers], [number])
        self.assertFalse(found.refresh)
        self.assertFalse(translated.facts)

    def test_admin_decision_can_be_confirmed_directly_but_ordinary_account_cannot_set_it(self):
        self.members.owner_wxid = "wxid_host"
        self.members.record_bot_reply(ROOM, "夜骑在东门集合。")
        self.model.proposal = {"范围": "本群", "对象": "夜骑", "关系": "集合地点", "事实": "这次夜骑在北门集合。",
                               "类别": "状态", "消息编号": 2, "依据类型": "群决定", "常见问法": ["夜骑在哪碰头"]}
        self.model.replies = ["是北门。", "夜骑到北门碰头。"]
        with patch("plugins.mention_chat.gather_answer_material") as gather:
            self.send("你刚才说错了，东门是旧方案，这次夜骑定北门。", sender="wxid_host")
            saved, = self.store.list(ROOM)
            self.members.reset_context(ROOM)
            self.fixture.message("刚才问的是夜骑。")
            request = self.send("那大伙儿在哪碰头？", sender="wxid_next")
        gather.assert_not_called()
        self.assertIn("北门", self.fixture.backend.texts[-1][1])
        self.assertEqual(self.store.used(ROOM, (self.members.message_id(request),)), {saved.id})
        self.assertTrue(saved.observed_on)

        invalid = self.fixture.message("旧方案取消，听我的去西门。")
        self.model.proposal.update(消息编号=self.members.message_id(invalid), 事实="夜骑改在西门。")
        self.send("旧方案取消了，你就按西门回答。")
        self.assertEqual([fact.statement for fact in self.store.list(ROOM)], [saved.statement])

    def test_version_fact_can_be_learned_and_reused_without_inventing_a_date(self):
        self.members.record_bot_reply(ROOM, "Python 3.14 自由线程仍是实验性的。")
        self.model.proposal = {"范围": "公开", "对象": "Python 3.14", "关系": "自由线程支持状态",
                               "事实": "Python 3.14 的自由线程构建已获官方支持，仍是可选项。",
                               "类别": "限定事实", "适用范围": "Python 3.14 自由线程构建",
                               "网址": PYTHON_URL, "常见问法": ["Python 3.14 的无 GIL 构建正式支持了吗"]}
        self.model.replies = ["Python 3.14 已正式支持自由线程构建。", "3.14 的无 GIL 构建已正式支持，仍是可选项。"]
        with (patch.object(self.fixture.magpie, "fetch", return_value=MagpieResult(PYTHON_SOURCE)) as fetch,
              patch.object(MentionTools, "search") as search):
            self.send("你搞错了，Python 3.14 自由线程已获官方支持。" + PYTHON_URL)
            saved, = self.store.list(ROOM)
            self.members.reset_context(ROOM)
            request = self.send("Python 3.14 的无 GIL 构建现在能算正式支持了吗？", sender="wxid_next")
        fetch.assert_called_once_with(PYTHON_URL)
        search.assert_not_called()
        self.assertEqual((saved.effective_on, saved.observed_on), ("", ""))
        self.assertEqual(saved.material()["适用范围"], "Python 3.14 自由线程构建")
        self.assertIn("仍是可选项", self.fixture.backend.texts[-1][1])
        self.assertEqual(self.store.used(ROOM, (self.members.message_id(request),)), {saved.id})

    def test_newer_group_decision_wins_on_the_same_day(self):
        fields = dict(scope="本群", subject_key="夜骑", subject="夜骑", relation="集合地点", kind="状态",
                      names=("夜骑",), phrases=(), effective_on="2026-11-22", observed_on="2026-11-22")
        def confirm(message_id, gate, correction_id):
            statement = f"这次夜骑在{gate}集合。"
            return self.store.record(ROOM, {**fields, "statement": statement, "source": {
                "消息编号": message_id, "正文": statement, "类型": "群决定"}}, correction_id)

        number = confirm(3, "东门", 3)
        self.assertEqual(confirm(5, "北门", 5), number)
        self.assertEqual(confirm(3, "东门", 7), number)
        fact, = self.store.list(ROOM)
        self.assertIn("北门", fact.statement)
        self.assertEqual(fact.source["消息编号"], 5)

    def test_message_with_primary_source_gets_the_lookup_budget(self):
        self.assertEqual(reply_seconds(f"看最新公告，之前答错了。{URL}"), 120)
        self.assertEqual(reply_seconds("英伟达的老黄辞职了？"), 90)
