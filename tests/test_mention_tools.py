"""艾特回复的看图 / 联网：图片索引、引用图片解析、工具编排、模型输入与端到端发送（不连网）。"""
import io
import json
import logging
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins.mention_chat import Plugin
from tests.test_mention_chat import ROOM, FakeCtx, FakeMembers, mention
from wxbot_framework.conversation import utterance
from wxbot_framework.conversation.dialogue import SYSTEM, Dialogue
from wxbot_framework.conversation.members import ConversationSnapshot
from wxbot_framework.conversation.mention_tools import (ASK_FOR_IMAGE, ASK_WHICH_IMAGE, NO_IMAGE_SUPPORT,
                                                        QUOTED_IMAGE_MISSING, MentionTools, extract_sources,
                                                        read_tool_options, strip_tracking, with_sources)
from wxbot_framework.conversation.tool_router import JevToolRouter
from wxbot_framework.integrations.jev import JevClient
from wxbot_framework.integrations.magpie_tools import (MagpieResult, MagpieTools, MagpieToolsUnavailableError,
                                                       _McpEndpoint)
from wxbot_framework.messaging.mentions import parse_quote
from wxbot_framework.recent_images import RECENT_IMAGES, RecentImages

NATIVE = Path(__file__).resolve().parent.parent / "native"
SENDER = "wxid_player"
OTHER = "wxid_other"
ROBOT = "wxid_robot"
MD5 = "fedcba9876543210fedcba9876543210"
AESKEY = "0123456789abcdef0123456789abcdef"
SEARCH_TEXT = ("Python 3.14 已发布（[python.org](https://www.python.org/downloads/?utm_source=openai)）。\n"
               "来源：[Release](https://docs.python.org/3/whatsnew/3.14.html?utm_source=openai&x=1)")
LOGGER = logging.getLogger("MentionToolsTests")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def image(index, *, room=ROOM, sender=SENDER, status="ready", message_id=None, md5="", path=None, error=""):
    data = {"chat_id": room, "sender_id": sender, "image_status": status, "sequence": message_id or 1,
            "native_message_id": str(message_id or ""), "image_metadata": {"md5": md5} if md5 else {},
            "image_path": path or f"C:/images/{message_id or 1}.png", "image_error": error}
    return index.note(data)


class FakeJev:
    """按问题类型给概率：instructions 里含“图片”算看图判断，否则算搜索判断。"""

    def __init__(self, vision=0.0, search=0.0, error=None):
        self.vision, self.search, self.error = vision, search, error
        self.calls, self.timeouts = [], []

    def noul(self, state, questions, *, timeout=None):
        kind = "vision" if "图片" in questions[0]["instructions"] else "search"
        self.calls.append((kind, state))
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return [self.vision if kind == "vision" else self.search]


class FakeMagpie:
    max_result_chars = 4000

    def __init__(self, vision="图里是一只橘猫，牌子上写着 199 元", search=SEARCH_TEXT, error=None):
        self.vision_text, self.search_text, self.error = vision, search, error
        self.images, self.queries, self.timeouts = [], [], []

    def analyze_image(self, path, prompt="", *, max_chars=None, timeout=None):
        self.images.append((path, prompt))
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return MagpieResult(self.vision_text)

    def search(self, query, *, max_chars=None, timeout=None):
        self.queries.append(query)
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return MagpieResult(self.search_text)


def tools(index, jev=None, magpie=None, *, supported=True, clock=None, budget=60):
    magpie = magpie if magpie is not None else FakeMagpie()
    return MentionTools(router=JevToolRouter(jev), magpie=lambda: magpie, images=index,
                        image_supported=supported, logger=LOGGER, clock=clock or Clock(), budget=budget), magpie


class RecentImagesTests(unittest.TestCase):
    def test_pending_update_keeps_first_seen_time_and_fills_path(self):
        clock = Clock(10)
        index = RecentImages(clock=clock)
        image(index, status="pending", message_id=5, path="")
        clock.now = 13
        record = image(index, status="ready", message_id=5, md5=MD5.upper(), path="C:/images/5.png")
        self.assertEqual((record.status, record.seen_at, record.path, record.md5), ("ready", 10, "C:/images/5.png", MD5))
        self.assertEqual(len(index.recent(ROOM, SENDER, before=20, within=120)), 1)

    def test_failed_record_keeps_reason_but_no_path(self):
        record = image(RecentImages(), status="failed", message_id=6, error="图片下载或保存失败")
        self.assertEqual((record.path, record.error), ("", "图片下载或保存失败"))

    def test_quote_matches_message_id_first_then_md5(self):
        index = RecentImages(clock=Clock())
        image(index, message_id=1, md5=MD5)
        image(index, message_id=2, md5="0" * 32)
        self.assertEqual(index.quoted(ROOM, svrid="2", md5=MD5).native_message_id, "2")
        self.assertEqual(index.quoted(ROOM, svrid="999", md5=MD5).native_message_id, "1")
        self.assertIsNone(index.quoted(ROOM, svrid="999", md5="1" * 32))
        self.assertIsNone(index.quoted("other@chatroom", svrid="1"))

    def test_recent_is_limited_to_sender_and_window(self):
        clock = Clock(100)
        index = RecentImages(clock=clock)
        image(index, message_id=1)
        image(index, message_id=2, sender=OTHER)
        clock.now = 300
        image(index, message_id=3)
        self.assertEqual([r.native_message_id for r in index.recent(ROOM, SENDER, before=310, within=120)], ["3"])
        # 提问之后才到的图片不算这次提问附带的图。
        self.assertEqual(index.recent(ROOM, SENDER, before=50, within=120), ())

    def test_invalid_events_and_bounds(self):
        index = RecentImages(per_room=2, rooms=2)
        for data in (None, {}, {"chat_id": ROOM, "image_status": "ready"},
                     {"chat_id": ROOM, "sequence": 1, "image_status": "done"}):
            self.assertIsNone(index.note(data))
        for number in range(1, 4):
            image(index, message_id=number)
        self.assertEqual([r.native_message_id for r in index.recent(ROOM, SENDER, before=1e9, within=1e9)], ["2", "3"])
        image(index, room="a@chatroom", message_id=1)
        image(index, room="b@chatroom", message_id=1)
        self.assertIsNone(index.quoted(ROOM, svrid="3"))

    def test_wait_returns_when_download_finishes(self):
        index = RecentImages()
        pending = image(index, status="pending", message_id=8, path="")
        timer = threading.Timer(0.05, lambda: image(index, status="ready", message_id=8))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(index.wait(pending, 3).status, "ready")
        self.assertEqual(index.wait(image(index, status="pending", message_id=9), 0.01).status, "pending")


class QuoteParsingTests(unittest.TestCase):
    def quote_xml(self, ref_type="3", svrid="1234567890123"):
        inner = (f'&lt;msg&gt;&lt;img aeskey="{AESKEY}" cdnthumbmd5="{"a" * 32}" md5="{MD5.upper()}" '
                 'cdnmidimgurl="https://cdn/mid" /&gt;&lt;/msg&gt;')
        return (f"<msg><appmsg><title>这是啥</title><type>57</type><refermsg><type>{ref_type}</type>"
                f"<svrid>{svrid}</svrid><fromusr>{ROOM}</fromusr><chatusr>{OTHER}</chatusr>"
                f"<displayname>小红</displayname><content>{OTHER}:\n{inner}</content></refermsg></appmsg></msg>")

    def test_image_quote_carries_svrid_and_md5_without_secrets(self):
        quote = parse_quote(self.quote_xml())
        self.assertEqual((quote["ref_type"], quote["svrid"], quote["image_md5"], quote["text"]),
                         ("3", "1234567890123", MD5, ""))
        self.assertNotIn(AESKEY, repr(quote))
        self.assertNotIn("cdn/mid", repr(quote))

    def test_non_image_quote_has_no_md5_and_bad_svrid_is_dropped(self):
        self.assertEqual(parse_quote(self.quote_xml(ref_type="1"))["image_md5"], "")
        self.assertEqual(parse_quote(self.quote_xml(svrid="12ab"))["svrid"], "")

    def test_native_quote_mapping_and_log_do_not_leak_image_keys(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        from filehelper_service import ReceivedTextMessage
        from text_service import NativeTextService
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        message = ReceivedTextMessage(type=49, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                      text=(SENDER + ":\n" + self.quote_xml()).encode())
        with self.assertLogs("NativeText", "INFO") as logs:
            data = service._map_message(message)
        self.assertEqual({key: data["quote"][key] for key in ("ref_type", "svrid", "image_md5")},
                         {"ref_type": "3", "svrid": "1234567890123", "image_md5": MD5})
        self.assertFalse(any(AESKEY in line or "cdn/mid" in line for line in logs.output))


class NativeIndexTests(unittest.TestCase):
    def setUp(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        from filehelper_service import ReceivedMediaMessageV4
        from text_service import NativeTextService
        self.message_type = ReceivedMediaMessageV4
        self.service = NativeTextService(pid=123, image_allowlist=())
        self.service.router.set_account(ROBOT)
        RECENT_IMAGES.clear()
        self.addCleanup(RECENT_IMAGES.clear)

    def test_submitted_image_is_pending_then_updated_on_delivery(self):
        submitted = []
        self.service._ensure_image_receiver = lambda: type("R", (), {"submit": lambda _, data: submitted.append(data)})()
        xml = f'<msg><img aeskey="{AESKEY}" cdnmidimgurl="https://cdn/mid" md5="{MD5}" /></msg>'
        message = self.message_type(type=3, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                    image_xml=(SENDER + ":\n" + xml).encode(), sequence=7, native_message_id=4242)
        self.assertIsNone(self.service._map_message(message))
        record = RECENT_IMAGES.quoted(ROOM, svrid="4242")
        self.assertEqual((record.status, record.sender), ("pending", SENDER))
        # 下载中的图也能按 md5 找到：引用里的编号对不上时不至于报“没收到”。
        self.assertIs(RECENT_IMAGES.quoted(ROOM, svrid="1", md5=MD5), record)
        data = dict(submitted[0], image_status="ready", image_path="C:/images/x.png", image_metadata={"md5": MD5})
        data.pop("raw_content")
        with self.assertLogs("NativeText", "INFO") as logs:
            self.service._deliver_image(data)
        record = RECENT_IMAGES.quoted(ROOM, md5=MD5)
        self.assertEqual((record.status, record.path), ("ready", "C:/images/x.png"))
        self.assertFalse(any("C:/images" in line for line in logs.output))


class MentionToolsTests(unittest.TestCase):
    def run_tools(self, question, *, index=None, jev=None, magpie=None, quote=None, supported=True, **options):
        index = index if index is not None else RecentImages(clock=Clock())
        runner, magpie = tools(index, jev, magpie, supported=supported, **options)
        return runner.run(question=question, room=ROOM, sender=SENDER, received_at=1000, quote=quote), magpie

    def test_plain_question_uses_no_tool(self):
        jev = FakeJev(search=0.2)
        outcome, magpie = self.run_tools("晚上吃啥", jev=jev)
        self.assertFalse(outcome.used)
        self.assertIsNone(outcome.ask)
        self.assertEqual([kind for kind, _ in jev.calls], ["search"])  # 没有图片就不问看图
        self.assertEqual((magpie.images, magpie.queries), ([], []))

    def test_explicit_search_runs_without_jev_and_extracts_clean_sources(self):
        outcome, magpie = self.run_tools("帮我搜一下 Python 最新版本", jev=None)
        self.assertEqual(magpie.queries, ["帮我搜一下 Python 最新版本"])
        self.assertTrue(outcome.results[0].ok)
        self.assertEqual(outcome.sources, ("https://www.python.org/downloads/",
                                           "https://docs.python.org/3/whatsnew/3.14.html?x=1"))

    def test_jev_search_respects_threshold(self):
        self.assertTrue(self.run_tools("Python 最新版本是多少", jev=FakeJev(search=0.8))[0].used)
        self.assertFalse(self.run_tools("Python 最新版本是多少", jev=FakeJev(search=0.65))[0].used)

    def test_jev_failure_does_not_call_tools_for_vague_questions(self):
        index = RecentImages(clock=Clock())
        image(index, message_id=1)
        outcome, magpie = self.run_tools("这个现在多少钱", index=index, jev=FakeJev(error=TimeoutError()))
        self.assertFalse(outcome.used)
        self.assertEqual((magpie.images, magpie.queries), ([], []))

    def test_single_recent_image_is_seen_then_feeds_search(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1, path="C:/images/cat.png")
        jev = FakeJev(vision=0.9, search=0.9)
        outcome, magpie = self.run_tools("这个现在多少钱", index=index, jev=jev)
        self.assertEqual(magpie.images[0][0], "C:/images/cat.png")
        self.assertIn("这个现在多少钱", magpie.images[0][1])
        self.assertEqual(jev.calls[1][1]["看图结果"], "图里是一只橘猫，牌子上写着 199 元")
        self.assertIn("199 元", magpie.queries[0])
        self.assertEqual([item.tool for item in outcome.results], ["vision", "search"])

    def test_image_not_needed_when_jev_says_no(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1)
        outcome, magpie = self.run_tools("晚安", index=index, jev=FakeJev(vision=0.3))
        self.assertEqual(magpie.images, [])
        self.assertFalse(outcome.used)

    def test_several_recent_images_ask_which_one(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1)
        image(index, message_id=2)
        outcome, magpie = self.run_tools("看看图", index=index)
        self.assertEqual(outcome.ask, ASK_WHICH_IMAGE)
        self.assertEqual(magpie.images, [])
        # 不需要看图的提问不因为有多张图而追问。
        self.assertIsNone(self.run_tools("晚安", index=index, jev=FakeJev(vision=0.1))[0].ask)

    def test_seen_image_is_not_a_candidate_again_but_can_be_quoted(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1, md5=MD5, path="C:/images/1.png")
        self.assertTrue(self.run_tools("看看图里是啥", index=index)[0].results[0].ok)
        image(index, message_id=2, path="C:/images/2.png")
        outcome, magpie = self.run_tools("看看图里是啥", index=index)
        self.assertIsNone(outcome.ask)  # 第一张已看过，只剩第二张，不用追问
        self.assertEqual(magpie.images[0][0], "C:/images/2.png")
        # 都看过后不再有候选，引用旧图仍能再看。
        self.assertEqual(self.run_tools("看看图里是啥", index=index)[0].ask, ASK_FOR_IMAGE)
        quote = {"ref_type": "3", "svrid": "1", "image_md5": MD5}
        outcome, magpie = self.run_tools("这是啥", index=index, quote=quote)
        self.assertEqual(magpie.images[0][0], "C:/images/1.png")

    def test_failed_vision_keeps_image_available_for_retry(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1)
        runner, magpie = tools(index, None, FakeMagpie(error=RuntimeError()))
        outcome = runner.run(question="看看图里是啥", room=ROOM, sender=SENDER, received_at=1000)
        self.assertFalse(outcome.results[0].ok)
        self.assertEqual(len(index.recent(ROOM, SENDER, before=1000, within=120)), 1)

    def test_explicit_vision_without_image(self):
        self.assertEqual(self.run_tools("看看图里是啥")[0].ask, ASK_FOR_IMAGE)
        self.assertEqual(self.run_tools("看看图里是啥", supported=False)[0].ask, NO_IMAGE_SUPPORT)

    def test_other_senders_image_is_not_used_without_quote(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1, sender=OTHER)
        self.assertEqual(self.run_tools("看看图里是啥", index=index)[0].ask, ASK_FOR_IMAGE)

    def test_quoted_image_is_seen_without_asking_jev(self):
        index = RecentImages(clock=Clock(10))
        image(index, message_id=77, sender=OTHER, md5=MD5, path="C:/images/q.png")
        jev = FakeJev(vision=0.0, search=0.0)
        outcome, magpie = self.run_tools("哈哈", index=index, jev=jev, quote={"ref_type": "3", "svrid": "1", "image_md5": MD5})
        self.assertEqual(magpie.images[0][0], "C:/images/q.png")
        self.assertNotIn("vision", [kind for kind, _ in jev.calls])
        self.assertTrue(outcome.results[0].ok)

    def test_missing_or_failed_quoted_image_is_reported_truthfully(self):
        quote = {"ref_type": "3", "svrid": "1", "image_md5": MD5}
        outcome, magpie = self.run_tools("这是啥", quote=quote)
        self.assertEqual((outcome.ask, outcome.results), (QUOTED_IMAGE_MISSING, []))
        self.assertEqual(self.run_tools("这是啥", quote=quote, supported=False)[0].ask, NO_IMAGE_SUPPORT)
        index = RecentImages(clock=Clock())
        image(index, message_id=1, status="failed", md5=MD5, error="图片下载或保存失败")
        outcome, magpie = self.run_tools("这是啥", index=index, quote={"ref_type": "3", "image_md5": MD5})
        self.assertEqual(outcome.results[0].note, "图片没下载成功：图片下载或保存失败")
        self.assertEqual(magpie.images, [])

    def test_pending_image_is_awaited(self):
        index = RecentImages()
        record = image(index, status="pending", message_id=3, path="")
        index.recent = lambda *args, **kwargs: (record,)
        timer = threading.Timer(0.05, lambda: image(index, status="ready", message_id=3, path="C:/images/3.png"))
        timer.start()
        self.addCleanup(timer.cancel)
        outcome, magpie = self.run_tools("识图", index=index)
        self.assertEqual(magpie.images[0][0], "C:/images/3.png")

    def test_tool_errors_become_short_notes(self):
        error = MagpieToolsUnavailableError("C:/secret/path 连接被拒绝")
        outcome, _ = self.run_tools("帮我搜一下天气", magpie=FakeMagpie(error=error))
        self.assertEqual(outcome.results[0].note, "工具服务连不上或超时")
        self.assertEqual(outcome.sources, ())

    def test_every_call_is_bounded_by_the_remaining_budget(self):
        index = RecentImages(clock=Clock(990))
        image(index, message_id=1)
        jev, clock = FakeJev(vision=0.9, search=0.9), Clock()
        outcome, magpie = self.run_tools("这个现在多少钱", index=index, jev=jev, clock=clock, budget=30)
        self.assertEqual(len(magpie.timeouts), 2)
        self.assertTrue(all(0 < value <= 30 for value in magpie.timeouts + jev.timeouts))
        self.assertTrue(all(value <= 10 for value in jev.timeouts))
        # 剩余时间不够判断加一次调用时，含糊提问不再问 Jev，也不调用工具。
        jev = FakeJev(search=0.9)
        outcome, magpie = self.run_tools("Python 最新版本是多少", jev=jev, budget=8.5)
        self.assertEqual((jev.calls, outcome.used), ([], False))

    def test_hung_jev_or_tool_is_abandoned_at_the_deadline(self):
        class Hung:
            max_result_chars = 4000

            def __init__(self):
                self.release = threading.Event()

            def noul(self, state, questions, *, timeout=None):
                self.release.wait(5)  # 客户端自己的超时失效时（如慢发响应头）也不能拖住这一轮
                return [0.9]

            def search(self, query, *, max_chars=None, timeout=None):
                self.release.wait(5)
                return MagpieResult("迟到的结果")

        hung = Hung()
        self.addCleanup(hung.release.set)
        module = "wxbot_framework.conversation.mention_tools."
        with patch(module + "MIN_CALL_SECONDS", 0.05), patch(module + "MIN_JEV_SECONDS", 0.01):
            started = time.monotonic()
            runner, _ = tools(RecentImages(), hung, hung, clock=time.monotonic, budget=0.3)
            outcome = runner.run(question="Python 最新版本是多少", room=ROOM, sender=SENDER, received_at=0)
            self.assertFalse(outcome.used)
            runner, _ = tools(RecentImages(), None, hung, clock=time.monotonic, budget=0.3)
            outcome = runner.run(question="帮我搜一下天气", room=ROOM, sender=SENDER, received_at=0)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(outcome.results[0].note, "工具服务连不上或超时")

    def test_unavailable_client_and_exhausted_budget(self):
        index = RecentImages(clock=Clock())
        runner = MentionTools(router=JevToolRouter(None), magpie=lambda: (_ for _ in ()).throw(ValueError("坏配置")),
                              images=index, image_supported=True, logger=LOGGER, clock=Clock())
        outcome = runner.run(question="搜一下天气", room=ROOM, sender=SENDER, received_at=1000)
        self.assertEqual(outcome.results[0].note, "工具服务不可用")
        outcome, magpie = self.run_tools("搜一下天气", budget=5)
        self.assertEqual(outcome.results[0].note, "这轮时间不够，没来得及调用")
        self.assertEqual(magpie.queries, [])


class ClientTimeoutTests(unittest.TestCase):
    """调用方给的剩余时间只能缩短客户端配置的超时。"""

    def test_jev_timeout_narrows_but_never_extends(self):
        client = JevClient({"base_url": "http://127.0.0.1:3425/v1", "api_key": "k", "timeout_seconds": 20})
        question = [{"instructions": "问", "true": "是", "false": "否"}]
        for timeout, expected in ((3, 3), (90, 20), (None, 20)):
            body = io.BytesIO(json.dumps({"answers": {"q0": {"noul": 0.4}}}).encode("utf-8"))
            with self.subTest(timeout=timeout), \
                    patch("wxbot_framework.integrations.jev.urlopen", return_value=body) as call:
                self.assertEqual(client.noul({}, question, timeout=timeout), [0.4])
                self.assertEqual(call.call_args.kwargs["timeout"], expected)
        with self.assertRaises(ValueError):
            client.noul({}, question, timeout=0)

    def test_jev_slow_body_is_cut_at_the_deadline(self):
        class Slow:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, size):
                self.reads += 1
                time.sleep(0.02)
                return b" "

        client = JevClient({"base_url": "http://127.0.0.1:3425/v1", "api_key": "k"})
        started = time.monotonic()
        with patch("wxbot_framework.integrations.jev.urlopen", return_value=Slow()):
            with self.assertRaisesRegex(Exception, "超时"):
                client.noul({}, [{"instructions": "问", "true": "是", "false": "否"}], timeout=0.15)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_magpie_timeout_narrows_the_total_deadline(self):
        endpoint = _McpEndpoint("http://127.0.0.1:3425/vision/mcp", timeout=30)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=TimeoutError("slow")) as call:
            with self.assertRaises(MagpieToolsUnavailableError):
                endpoint.call("analyze_image", {"image": "x"}, timeout=2)
        self.assertLessEqual(call.call_args.kwargs["timeout"], 2)
        with self.assertRaises(ValueError):
            MagpieTools().search("天气", timeout=0)


class HelperTests(unittest.TestCase):
    def test_sources(self):
        self.assertEqual(strip_tracking("https://a.com/x?utm_source=openai&id=3#top"), "https://a.com/x?id=3")
        self.assertEqual(strip_tracking("javascript:alert(1)"), "")
        self.assertEqual(extract_sources("见 https://a.com/1 和 https://a.com/1。"), ("https://a.com/1",))
        self.assertEqual(extract_sources("https://a.com/" + "x" * 400 + " https://b.com"), ("https://b.com",))
        # 字数不多但字节超限的中文链接同样不收。
        self.assertEqual(extract_sources("https://a.com/" + "中" * 100 + " https://b.com"), ("https://b.com",))
        self.assertEqual(with_sources("答案", ()), "答案")
        self.assertEqual(with_sources("答案", ("https://a.com",)), "答案\n来源：\nhttps://a.com")

    def test_read_tool_options(self):
        self.assertEqual(read_tool_options({}), {"vision_threshold": 0.6, "search_threshold": 0.7})
        self.assertIsNone(read_tool_options({"tools": {"enabled": False}}))
        self.assertEqual(read_tool_options({"tools": {"search_threshold": 0.8}})["search_threshold"], 0.8)
        for config in ({"tools": []}, {"tools": {"enabled": "yes"}}, {"tools": {"vision_threshold": 2}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                read_tool_options(config)


class DialogueToolSectionTests(unittest.TestCase):
    class Client:
        def complete_text(self, system, user, **options):
            self.system, self.user = system, user
            return "好"

    def test_sections_are_rendered_only_when_given(self):
        client = self.Client()
        snapshot = ConversationSnapshot(ROOM, "2026-10-29", 0, "小明", (), "这是啥", ())
        Dialogue(client).reply(snapshot, tool_results=[
            {"tool": "vision", "ok": True, "text": "一只猫", "note": ""},
            {"tool": "search", "ok": False, "text": "", "note": "工具服务连不上或超时"}])
        self.assertIn("【看图结果】一只猫", client.user)
        self.assertIn("【联网搜索结果】\n（没成功）工具服务连不上或超时", client.user)
        Dialogue(client).reply(snapshot)
        self.assertNotIn("【看图结果】", client.user)
        self.assertNotIn("【联网搜索结果】", client.user)

    def test_system_forbids_claiming_unseen_images_or_searches(self):
        self.assertIn("只有出现【看图结果】时才能说看了图", SYSTEM)
        self.assertIn("不要自己写网址", SYSTEM)


class MentionChatToolTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(utterance.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        RECENT_IMAGES.clear()
        self.addCleanup(RECENT_IMAGES.clear)

    def build(self, reply, *, jev=None, magpie=None):
        members = FakeMembers()
        ctx = FakeCtx(members, reply)
        ctx.jev, ctx.magpie = jev, magpie or FakeMagpie()
        ctx.capabilities = frozenset({"group.image.receive"})
        plugin = Plugin()
        plugin.members, plugin.room_state = members, None
        plugin.tools = read_tool_options({})
        return plugin, ctx

    def test_search_reply_is_one_message_with_sources(self):
        plugin, ctx = self.build("3.14 已经发了。没错。", jev=FakeJev(search=0.9))
        plugin.on_message(mention("Python 最新版本是多少"), ctx)
        self.assertIn("【联网搜索结果】", ctx.last_user)
        self.assertEqual(ctx.sent, [("reply_at", "3.14 已经发了。没错。\n来源：\nhttps://www.python.org/downloads/\n"
                                                 "https://docs.python.org/3/whatsnew/3.14.html?x=1")])

    def test_failed_search_is_told_to_model_without_sources(self):
        plugin, ctx = self.build("没查到，搜索挂了", magpie=FakeMagpie(error=MagpieToolsUnavailableError("x")))
        plugin.on_message(mention("帮我搜一下今天的新闻"), ctx)
        self.assertIn("（没成功）工具服务连不上或超时", ctx.last_user)
        self.assertEqual(ctx.sent, [("reply_at", "没查到，搜索挂了")])

    def test_ambiguous_images_get_a_fixed_question_without_model_call(self):
        plugin, ctx = self.build("不该调用")
        for number in (1, 2):
            RECENT_IMAGES.note({"chat_id": ROOM, "sender_id": SENDER, "image_status": "ready", "sequence": number,
                                "native_message_id": str(number), "image_path": f"C:/images/{number}.png"})
        plugin.on_message(mention("看看图", received_at=time.monotonic()), ctx)
        self.assertEqual(ctx.sent, [("reply_at", ASK_WHICH_IMAGE)])
        self.assertEqual(ctx.completions, 0)

    def test_disabled_tools_leave_the_reply_path_unchanged(self):
        plugin, ctx = self.build("好的")
        plugin.tools = None
        plugin.on_message(mention("帮我搜一下天气"), ctx)
        self.assertNotIn("【联网搜索结果】", ctx.last_user)
        self.assertEqual(ctx.magpie.queries, [])

    def test_image_events_are_not_answered(self):
        plugin, ctx = self.build("不该调用")
        from wxbot_framework.sdk import Message
        event = Message("", ROOM, sender_id=SENDER, type=3, is_group=True,
                        data={"wx_type": 3, "image_status": "ready", "is_at_self": True, "at_members": (ROBOT,)})
        plugin.on_message(event, ctx)
        self.assertEqual(ctx.sent, [])


if __name__ == "__main__":
    unittest.main()
