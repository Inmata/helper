"""真实本机 HTTP 契约、插件入口、后台图片投递与停止守卫。"""
import base64
import json
import logging
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from support.recyclable_tempdir import RecyclableTemporaryDirectory
from plugins.mention_chat import Plugin
from wxbot_framework.authorization import LOCAL_EDITOR
from wxbot_framework.messaging.mentions import strip_at_labels
from wxbot_framework.conversation.members import MemberStore
from wxbot_framework.conversation.status_replies import StatusReplies
from wxbot_framework.integrations.jev import JevError
from wxbot_framework.integrations.llm import LLMClient, LLMContentBlocked, LLMError
from wxbot_framework.integrations.magpie_images import MagpieImages
from wxbot_framework.integrations.magpie_tools import MagpieTools, MagpieToolsToolError
from wxbot_framework.memory.knowledge import KnowledgeStore
from wxbot_framework.recent_images import RECENT_IMAGES
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOM = "12345678@chatroom"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in request:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if request["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "magpie-image-contract", "version": "1"}}
        else:
            params = request["params"]
            self.server.calls.append(params)
            if params["name"] in self.server.errors:
                result = {"content": [{"type": "text", "text": self.server.errors[params["name"]]}],
                          "isError": True}
            elif params["name"] == "generate_image":
                self.server.submitted.set()
                result = {"_meta": {"usemagpie.ai/pending": "img_contract"},
                          "content": [{"type": "text", "text": "still being made"}], "isError": False}
            elif params["name"] == "analyze_image":
                result = {"content": [{"type": "text", "text": "左侧旧职业为软件开发，右侧为白底肖像"}]}
            else:
                self.server.waiting.set()
                self.server.release.wait(5)
                result = ({"content": [{"type": "text", "text": "generation failed"}], "isError": True}
                          if self.server.fail else
                          {"content": [{"type": "image", "mimeType": "image/png",
                                        "data": base64.b64encode(self.server.image).decode()}], "isError": False})
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class Backend:
    session_id = "connection-one"

    def __init__(self):
        self.texts, self.images = [], []
        self.image_success = True

    def send_text(self, room, text):
        self.texts.append((room, text))
        return True

    def send_at_text(self, room, text, members):
        return self.send_text(room, text)

    def send_image(self, room, path):
        self.images.append((room, Path(path).read_bytes()))
        return self.image_success

    def stop(self):
        return {"dll_unloaded": True}


class Jev:
    def noul(self, state, questions, **options):
        text = state.get("当前艾特正文")
        if text:
            values = [0.01, 0.01] if text in ("画不出来？", "图呢？", "画好了吗？") else [0.99, 0.01]
            if "请求人上一次生图要求" in state:
                values.append(0.95 if text == "以这个为准再生成一次" else 0.05)
            return values
        return [0.99, 0.01]


class ImageModels:
    def __init__(self):
        self.calls = []
        self.plans = []

    def complete_json(self, system, user, **options):
        self.plans.append(json.loads(user))
        return {"绘图指令": "水彩绘制一只橘猫，浅色背景"}

    def complete_text(self, system, user, **options):
        payload, _ = json.JSONDecoder().raw_decode(user)
        self.calls.append(payload)
        return "，".join(payload["实际状态"].values())


def lookup(tool_name, **arguments):
    return {"工具调用": [{"名称": tool_name, "参数": arguments}]}


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = RecyclableTemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls, self.server.image, self.server.fail = [], PNG, False
        self.server.errors = {}
        self.server.submitted, self.server.release = threading.Event(), threading.Event()
        self.server.waiting = threading.Event()
        self.server.release.set()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.magpie = MagpieTools({"base_url": "http://127.0.0.1:%d" % self.server.server_address[1]})
        self.client = MagpieImages(self.magpie, self.root / "output")
        config = self.root / "members.json"
        config.write_text(json.dumps({"database_file": "members.db", "history_enabled": True,
                                      "daily_clear": False, "groups": {ROOM: []}}), encoding="utf-8")
        self.members = MemberStore(config, "wxid_robot")
        self.members.initialize()
        self.knowledge = KnowledgeStore(self.members)
        self.knowledge.initialize()
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.ctx = PluginContext("mention_chat", {}, self.service,
                                 {"group.image.send", "group.image.receive", "group.text.mention"},
                                 member_store=self.members, knowledge_store=self.knowledge)
        self.ctx._magpie, self.ctx._jev = self.magpie, Jev()
        self.ctx._llm = ImageModels()
        self.plugin = Plugin()
        self.plugin.members, self.plugin.room_state = self.members, None
        self.plugin._status = StatusReplies(self.ctx.logger)
        self.output_patch = patch("plugins.mention_chat.image_reply.OUTPUT_DIR", self.root / "output")
        self.output_patch.start()
        self.plugin.image_reply.start(self.ctx, self.plugin._status)
        self.sequence = 0
        RECENT_IMAGES.clear()

    def tearDown(self):
        self.server.release.set()
        self.plugin.image_reply.close()
        self.plugin._status.close()
        self.output_patch.stop()
        self.service.close()
        self.members.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        RECENT_IMAGES.clear()
        self.directory.cleanup()

    def message(self, text="/生图 水彩橘猫", *, sender="wxid_member", **changes):
        self.sequence += 1
        message = Message(text, ROOM, sender_id=sender, is_group=True, sequence=self.sequence,
                          session_id=self.backend.session_id, **changes)
        self.members.record(message)
        return message

    def wait_done(self):
        self.wait_for(lambda: not self.plugin.has_pending_callbacks())

    def wait_for(self, done):
        deadline = time.monotonic() + 5
        while not done() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(done())

    def profile_fixture(self):
        self.members.register("老猫子", ROOM, "wxid_cat")
        self.message("老猫子报道", sender="wxid_cat")
        person = self.knowledge.member_entity(ROOM, "wxid_cat")
        self.knowledge.add_memory(ROOM, person["id"], "人物背景", "人工确认", "老猫子是北京人，三十多岁。", LOCAL_EDITOR)
        self.knowledge.add_memory(ROOM, person["id"], "台球厅与近况", "人工确认",
                                  "以前经营台球厅，店已倒闭。只作群聊背景。", LOCAL_EDITOR)
        with self.knowledge.connection() as db:
            for slot, kind, content in [
                ("战锤设定讨论", "本人陈述", "喜欢讨论战锤40K的设定"),
                ("宝可梦对战讨论", "本人陈述", "玩宝可梦双打"),
                ("AI配对", "群内玩笑", "群友调侃和机器人配CP"),
                *(("炒股近况", "本人陈述", f"目前在炒股，第{n}次讨论股价") for n in range(25)),
            ]:
                self.knowledge.put(ROOM, person["id"], slot, kind, content)
            db.execute("UPDATE knowledge_entities SET aliases=? WHERE id=?", (json.dumps(["猫老板"]), person["id"]))

    def poster_fixture(self):
        path = self.root / "poster.png"
        Image.new("RGB", (512, 512), color="white").save(path)
        self.server.image = path.read_bytes()
        RECENT_IMAGES.note({"chat_id": ROOM, "sender_id": "wxid_member", "native_message_id": "912",
                            "image_status": "ready", "image_path": str(path)})
        return path

    def test_mcp_pending_then_large_image_keeps_binary_and_one_submission(self):
        self.server.image = PNG + b"\0" * (2 * 1024 * 1024)
        first = self.client.submit("水彩橘猫", filename="large")
        self.assertEqual(first.task_id, "img_contract")
        result = self.client.result(first.task_id, filename="large")
        self.assertEqual(result.path.read_bytes(), self.server.image)
        self.assertEqual([call["name"] for call in self.server.calls], ["generate_image", "generation_result"])
        self.assertEqual(self.server.calls[0]["arguments"], {"prompt": "水彩橘猫", "n": 1})

    def test_tool_error_does_not_become_an_image(self):
        self.server.fail = True
        with self.assertRaises(MagpieToolsToolError):
            self.client.result("img_contract", filename="failed")
        self.assertFalse((self.root / "output" / "failed.png").exists())

    def test_safety_rejection_explains_reason_without_retry_or_raw_error(self):
        rejection = ("no image: api.openai.com: 400 Bad Request: Your request was rejected by the safety system. "
                     "If you believe this is an error, contact us at help.openai.com and include the request ID req_test.")
        for tool in ("generate_image", "generation_result"):
            with self.subTest(tool=tool):
                self.server.errors = {tool: rejection}
                self.server.calls.clear()
                self.backend.texts.clear()
                self.plugin.on_message(self.message(), self.ctx)
                self.wait_done()
                reply = self.backend.texts[-1][1]
                self.assertIn("安全审核拒绝", reply)
                self.assertNotIn("稍后再试", reply)
                self.assertNotIn("api.openai.com", reply)
                self.assertNotIn("req_test", reply)
                self.assertEqual(self.backend.images, [])
                self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)

    def test_command_delivers_image_and_suppresses_delayed_duplicate(self):
        self.server.release.clear()
        self.plugin.on_message(self.message(), self.ctx)
        self.assertTrue(self.server.submitted.wait(2))
        duplicate = self.message()
        self.plugin.on_message(duplicate, self.ctx)
        self.server.release.set()
        self.wait_done()
        self.plugin.on_message(duplicate, self.ctx)
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)
        self.assertEqual(self.server.calls[0]["arguments"]["prompt"], "水彩绘制一只橘猫，浅色背景")
        self.assertFalse(any("图片已发出" in text for _, text in self.backend.texts))

    def test_followup_uses_original_question_and_real_stage_when_intent_check_fails(self):
        self.server.release.clear()
        self.plugin.on_message(self.message(), self.ctx)
        self.assertTrue(self.server.waiting.wait(2))
        model = self.ctx._llm
        complete_text = model.complete_text
        noul = self.ctx._jev.noul
        outputs = {"画不出来？": "图还没回来，我再等等。", "图呢？": "这张还没到我手上呢。"}

        def render(system, user, **options):
            reply = complete_text(system, user, **options)
            question = json.JSONDecoder().raw_decode(user)[0]["群友原话"]
            return outputs.get(question, reply)

        def intent_unavailable(state, questions, **options):
            if "当前话语" in state:
                raise JevError("本机测试意图判断不可用")
            return noul(state, questions, **options)

        with (patch.object(model, "complete_text", side_effect=render),
              patch.object(self.ctx._jev, "noul", wraps=self.ctx._jev.noul) as judge):
            for question, expected in outputs.items():
                if question == "图呢？":
                    judge.side_effect = intent_unavailable
                self.plugin.on_message(self.message("@机器人\u2005" + question,
                    data={"is_at_self": True, "at_members": ["wxid_robot"]}), self.ctx)
                self.wait_for(lambda: any(text.endswith(expected) for _, text in self.backend.texts))
                self.assertIn({"群友原话": question, "实际状态": {"当前任务阶段": "等待生成服务返回图片"}}, model.calls)
            judge.side_effect = JevError("本机测试事实核对不可用")
            sent = list(self.backend.texts)
            self.plugin.on_message(self.message("@机器人\u2005图呢？",
                data={"is_at_self": True, "at_members": ["wxid_robot"]}), self.ctx)
            self.wait_for(lambda: not self.plugin._status.busy())
            self.assertEqual(self.backend.texts, sent)
        self.server.release.set()
        self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)

    def test_followup_generated_during_delivery_drops_old_stage(self):
        self.server.release.clear()
        self.plugin.on_message(self.message(), self.ctx)
        self.assertTrue(self.server.waiting.wait(2))
        model = self.ctx._llm
        complete_text = model.complete_text
        started, allow_reply = threading.Event(), threading.Event()

        def render(system, user, **options):
            if json.JSONDecoder().raw_decode(user)[0]["群友原话"] == "画好了吗？":
                started.set()
                allow_reply.wait(3)
                return "这张还没回来呢。"
            return complete_text(system, user, **options)

        with patch.object(model, "complete_text", side_effect=render):
            self.plugin.on_message(self.message("@机器人\u2005画好了吗？",
                data={"is_at_self": True, "at_members": ["wxid_robot"]}), self.ctx)
            self.assertTrue(started.wait(2))
            try:
                self.server.release.set()
                self.wait_for(lambda: not self.plugin.image_reply.busy())
            finally:
                allow_reply.set()
            self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        self.assertFalse(any(text.endswith("这张还没回来呢。") for _, text in self.backend.texts))

    def test_status_model_outage_does_not_prevent_image_delivery(self):
        with patch.object(self.ctx._llm, "complete_text", side_effect=LLMError("本机测试模型不可用")):
            self.plugin.on_message(self.message(), self.ctx)
            self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        self.assertEqual(self.backend.texts, [])

    def test_explicit_mention_is_owned_once_and_not_merged_into_previous_turn(self):
        previous = self.message("@机器人\u2005前一个问题", dispatch_sequence=1, dispatch_epoch="epoch")
        incoming = self.message("@机器人\u2005帮我画只猫", dispatch_sequence=2, dispatch_epoch="epoch",
                                data={"is_at_self": True, "at_members": ["wxid_robot"]})
        turns = self.members.turns
        turns.observe(previous)
        turn = turns.open(previous, "前一个问题", 0)
        turns.observe(incoming)
        self.assertFalse(self.plugin._advance_turn(turns, turn, self.ctx))
        self.assertFalse(turns.merged(incoming))
        turns.close(turn)
        self.plugin.on_message(incoming, self.ctx)
        self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_low_image_score_routes_comic_once_and_keeps_only_requester_context(self):
        prior = self.message("我想做侦探题材")
        self.message("今晚练二头", sender="wxid_other")
        mention = {"is_at_self": True, "at_members": ["wxid_robot"]}
        msg = self.message("@机器人\u2005做个四格悬置漫画，侦探调查失踪的泡面", data=mention,
                           dispatch_epoch="epoch", dispatch_sequence=2)
        plan = self.ctx._llm.complete_json
        choices = []

        def model(system, user, **options):
            payload = json.loads(user)
            if "当前发言" in payload:
                choices.append(payload)
                return {"动作": "create"}
            return plan(system, user, **options)

        with (patch.object(self.ctx._jev, "noul", return_value=[0.60, 0.05]),
              patch.object(self.ctx._llm, "complete_json", side_effect=model),
              self.assertLogs(self.ctx.logger, level="INFO") as log):
            turns = self.members.turns
            turn = turns.open(prior, prior.text, 0)
            turns.observe(msg)
            self.assertFalse(self.plugin._advance_turn(turns, turn, self.ctx))
            turns.close(turn)
            self.plugin.on_message(msg, self.ctx)
            self.wait_done()
        self.assertEqual([p["与请求人的近期往来"] for p in choices], [[{"作者": "请求人", "内容": prior.text}]])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        source = self.members.message_id(msg)
        self.assertTrue(any(f"消息={source} Jev=0.6" in row for row in log.output))
        self.assertTrue(any(f"消息={source} 来源=主模型 动作=create" in row for row in log.output))
        receipt = self.members.connection().execute("SELECT source_id,status FROM reply_receipts WHERE kind='image'").fetchone()
        self.assertEqual(tuple(receipt), (source, "submitted"))

    def test_format_clarification_resumes_original_request_and_reference(self):
        reference, _ = self.reference_fixture("source-picture")
        mention = {"is_at_self": True, "at_members": ["wxid_robot"], "quote": {"ref_type": "3", "svrid": "source-picture"}}
        original = self.message("@机器人\u2005生成这个故事", data=mention)
        with (patch.object(self.ctx._jev, "noul", side_effect=JevError("timeout")),
              patch.object(self.ctx._llm, "complete_json", return_value={"动作": "clarify"})):
            self.plugin.on_message(original, self.ctx)
        question = strip_at_labels(self.backend.texts[-1][1])
        self.assertIn("图片", question)
        self.assertEqual(self.server.calls, [])
        other = self.message("图片", sender="wxid_other")
        self.plugin.on_message(other, self.ctx)
        self.assertEqual(self.server.calls, [])
        reply = self.message("图片", data={"quote": {"fromusr": "wxid_robot", "ref_type": "1", "text": question}})
        self.plugin.on_message(reply, self.ctx)
        self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        self.assertEqual(self.ctx._llm.plans[-1]["任务"]["本次请求"], "生成这个故事")
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(reference)])

    def test_answer_text_to_format_question_uses_chat_with_original_prompt(self):
        original = self.message("@机器人\u2005生成这段冒险", data={"is_at_self": True, "at_members": ["wxid_robot"]})
        with (patch.object(self.ctx._jev, "noul", return_value=[0.27, 0.05]),
              patch.object(self.ctx._llm, "complete_json", return_value={"动作": "clarify"})):
            self.plugin.on_message(original, self.ctx)
        reply = self.message("我想要文字版")
        answer = "旅人终于找到了公主"
        with (patch.object(self.ctx._jev, "noul", side_effect=lambda state, questions, **kw: [0.01] * len(questions)),
              patch.object(self.ctx._llm, "complete_text", return_value=answer) as write):
            self.plugin.on_message(reply, self.ctx)
        self.assertTrue(any("生成这段冒险" in call.args[1] and "写成文字" in call.args[1]
                            for call in write.call_args_list))
        self.assertIn(answer, self.backend.texts[-1][1])
        self.assertEqual(self.server.calls, [])

    def test_likely_image_asks_instead_of_chat_when_second_opinion_times_out(self):
        mention = {"is_at_self": True, "at_members": ["wxid_robot"]}
        with (patch.object(self.ctx._jev, "noul", return_value=[0.60, 0.05]),
              patch.object(self.ctx._llm, "complete_json", side_effect=TimeoutError)):
            self.plugin.on_message(self.message("@机器人\u2005做个四格漫画", data=mention), self.ctx)
        self.assertIn("图片", self.backend.texts[-1][1])
        self.assertEqual(self.server.calls, [])

    def test_rejected_and_unavailable_format_choices_leave_drawing_idle(self):
        mention = {"is_at_self": True, "at_members": ["wxid_robot"]}
        with patch.object(self.ctx._jev, "noul", return_value=[0.05, 0.04]):
            for query, result in (("怎么画漫画？", {"动作": "text"}), ("别生成", {"动作": "text"}),
                                  ("生成这个", {"动作": "未知"})):
                msg = self.message("@机器人\u2005" + query, data=mention)
                with (patch.object(self.ctx._llm, "complete_json", return_value=result),
                      patch.object(self.plugin, "_reply_turn", return_value=iter(())) as chat):
                    self.plugin.on_message(msg, self.ctx)
                    chat.assert_called_once()
            with (patch.object(self.ctx._llm, "complete_json", side_effect=LLMError("timeout")),
                  patch.object(self.plugin, "_reply_turn", return_value=iter(())) as chat):
                self.plugin.on_message(self.message("@机器人\u2005聊会儿", data=mention), self.ctx)
                chat.assert_called_once()
        self.assertEqual(self.server.calls, [])

    def test_quoted_image_is_forwarded_as_exact_local_reference(self):
        reference = self.root / "reference.png"
        reference.write_bytes(PNG)
        RECENT_IMAGES.note({"chat_id": ROOM, "sender_id": "wxid_member", "native_message_id": "900",
                            "image_status": "ready", "image_path": str(reference)})
        self.plugin.on_message(self.message("/生图 把背景改成雪山", data={"quote": {"ref_type": "3", "svrid": "900"}}), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(reference.resolve())])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_quoted_redraw_starts_new_generation_after_previous_image_was_sent(self):
        original = "/生图 画一张漫画，主角在台上领奖"
        self.plugin.on_message(self.message(original), self.ctx)
        self.wait_done()
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        reference, _ = self.reference_fixture("new-portrait")
        self.reference_fixture("other-portrait", sender="wxid_other")
        self.message("/生图 画一张别的海报", sender="wxid_other")
        noul = self.ctx._jev.noul
        prepare = self.ctx._llm.complete_json

        def judge(state, questions, **options):
            if "当前话语" in state:
                return [0.9]
            return noul(state, questions, **options)

        def plan_timeout(system, user, **options):
            prepare(system, user, **options)
            raise TimeoutError

        with (patch.object(self.ctx._jev, "noul", side_effect=judge),
              patch.object(self.ctx._llm, "complete_json", side_effect=plan_timeout)):
            self.plugin.on_message(self.message("@机器人\u2005以这个为准再生成一次", data={
                "is_at_self": True, "at_members": ["wxid_robot"],
                "quote": {"ref_type": "3", "svrid": "new-portrait", "fromusr": "wxid_member"},
            }), self.ctx)
            self.wait_done()

        submissions = [call["arguments"] for call in self.server.calls if call["name"] == "generate_image"]
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[-1]["reference_images"], [str(reference)])
        self.assertEqual([row["内容"] for row in self.ctx._llm.plans[-1]["任务"]["请求人近期生图命令"]], [original])
        self.assertEqual(submissions[-1]["prompt"], "画一张漫画，主角在台上领奖\n以这个为准再生成一次")
        self.assertEqual(self.backend.images, [(ROOM, PNG), (ROOM, PNG)])

    def reference_fixture(self, native_id, *, sender="wxid_member", status="ready"):
        path = self.root / f"reference-{native_id}.png"
        path.write_bytes(PNG)
        data = {"chat_id": ROOM, "sender_id": sender, "native_message_id": native_id,
                "image_status": status, "image_path": str(path)}
        RECENT_IMAGES.note(data)
        return path, data

    def test_two_recent_references_keep_send_order_when_first_download_finishes_last(self):
        self.reference_fixture("old")
        first, first_data = self.reference_fixture("person", status="pending")
        second, _ = self.reference_fixture("clothes")
        self.reference_fixture("bystander", sender="wxid_other")
        with patch.object(self.ctx._llm, "complete_json", return_value={
            "绘图指令": "保留第一张的人和姿势，换上第二张的衣服", "参考图": "I2"
        }) as planner:
            self.plugin.on_message(self.message("/生图 让第一张的人穿上第二张的衣服"), self.ctx)
            RECENT_IMAGES.note({**first_data, "image_status": "ready"})
            self.wait_done()
        task = json.loads(planner.call_args.args[1])["任务"]
        self.assertEqual(task["本次参考图"], ["I1", "I2"])
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(first), str(second)])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_quoted_image_and_recent_own_image_reach_magpie_even_with_planning_failure(self):
        quoted, _ = self.reference_fixture("old-person", sender="wxid_other")
        recent, _ = self.reference_fixture("clothes")
        self.reference_fixture("bystander", sender="wxid_other")
        with patch.object(self.ctx._llm, "complete_json", side_effect=TimeoutError):
            self.plugin.on_message(self.message("/生图 让这张图里的人穿上我刚发的衣服",
                data={"quote": {"ref_type": "3", "svrid": "old-person"}}), self.ctx)
            self.wait_done()
        self.assertEqual([call["name"] for call in self.server.calls], ["generate_image", "generation_result"])
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(quoted), str(recent)])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_missing_second_reference_asks_for_image_and_resumes_after_upload(self):
        first, _ = self.reference_fixture("person")
        self.reference_fixture("bystander", sender="wxid_other")
        self.plugin.on_message(self.message("/生图 把这两张图合在一起"), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls, [])
        question = self.backend.texts[-1][1]
        self.assertIn("能把缺的图发到群里吗", question)
        second, _ = self.reference_fixture("clothes")
        self.plugin.on_message(self.message("/生图 补好了", data={
            "quote": {"ref_type": "1", "fromusr": self.members.account, "text": question}
        }), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(first), str(second)])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_named_profile_and_poster_copy_reach_image_model(self):
        self.profile_fixture()
        self.poster_fixture()
        drawing = "画一张复古台球厅风格海报，标题「老猫子征婚启事」，正文「三十来岁，开过球房，现在看盘」。"
        result = {"绘图指令": drawing, "依据": ["本次请求", "E32", "E33"], "检查项": ["用了复古风格"]}
        with patch.object(self.ctx._llm, "complete_json", return_value=result) as planner:
            self.plugin.on_message(self.message("/生图 老猫子征婚图"), self.ctx)
            self.wait_done()
        planner.assert_called_once()
        people = json.loads(planner.call_args.args[1])["任务"]["群友档案素材"]
        self.assertEqual([person["人物"] for person in people], ["老猫子"])
        self.assertEqual(set(people[0]["素材"]), {"基本情况", "经历", "近况", "兴趣", "群梗"})
        material = json.dumps(people, ensure_ascii=False)
        for word in ("北京人", "三十多岁", "台球厅", "倒闭", "炒股", "战锤", "宝可梦"):
            self.assertIn(word, material)
        self.assertIn("把字、人物和背景一起画进成品", planner.call_args.args[0])
        self.assertEqual([call["name"] for call in self.server.calls], ["generate_image", "generation_result"])
        self.assertEqual(self.server.calls[0]["arguments"]["prompt"], drawing)
        self.assertEqual(self.backend.images, [(ROOM, self.server.image)])

    def test_context_can_lookup_full_profile_by_alias_and_inspect_exact_edit_reference(self):
        self.profile_fixture()
        self.message("帮猫老板更新下面这张人物卡。")
        reference = self.poster_fixture()
        outputs = [
            {"工具调用": [{"名称": "read_profile", "参数": {"name": "猫老板"}},
                      {"名称": "inspect_image", "参数": {"image": "I1", "focus": "旧职业"}}]},
            {"绘图指令": "把左侧旧资料替换成「开过球房，现在看盘」，保留右侧肖像及其白底"},
        ]
        with patch.object(self.ctx._llm, "complete_json", side_effect=outputs) as planner:
            self.plugin.on_message(self.message("/生图 按刚才说的纠正这张图里的职业",
                data={"quote": {"ref_type": "3", "svrid": "912"}}), self.ctx)
            self.wait_done()
        self.assertEqual([call["name"] for call in self.server.calls],
                         ["analyze_image", "generate_image", "generation_result"])
        self.assertEqual(json.loads(planner.call_args_list[0].args[1])["任务"]["群友档案素材"], [])
        material, picture = json.loads(planner.call_args.args[1])["取材记录"][0]
        self.assertIn("以前经营台球厅", material["结果"]["档案"]["素材"]["经历"][0]["内容"])
        self.assertIn("左侧旧职业", picture["结果"]["观察"]["内容"])
        arguments = self.server.calls[1]["arguments"]
        self.assertIn("保留右侧肖像", arguments["prompt"])
        self.assertIn("开过球房，现在看盘", arguments["prompt"])
        self.assertEqual(arguments["reference_images"], [str(reference.resolve())])
        self.assertEqual(self.backend.images, [(ROOM, self.server.image)])

    def test_clarification_interrupts_generation_and_keeps_quoted_text(self):
        self.message("请沿用绿底", sender="wxid_member")
        with patch.object(self.ctx._llm, "complete_json", side_effect=[{"澄清问题": "这张卡片要画哪位群友？"}]) as planner:
            self.plugin.on_message(self.message("/生图 按这段话画个人物卡", data={"quote": {
                "ref_type": "1", "text": "用本人档案做一张简介", "name": "小林"}}), self.ctx)
            self.wait_done()
        payload = json.loads(planner.call_args.args[1])["任务"]
        self.assertEqual(payload["引用消息"]["内容"], "用本人档案做一张简介")
        self.assertIn("请沿用绿底", [row["内容"] for row in payload["近期收发对话"]])
        self.assertEqual(self.server.calls, [])
        self.assertIn("这张卡片要画哪位群友？", self.backend.texts[-1][1])

    def test_bad_json_does_not_ask_model_again_and_draws_with_prefetched_profile(self):
        self.profile_fixture()
        self.ctx._llm = LLMClient({"base_url": "http://127.0.0.1:1/v1", "model": "test", "api_key": "test"})
        with (patch.object(self.ctx._llm, "_post", return_value="不是 JSON") as post,
              patch.object(self.ctx._llm, "complete_text", return_value="已受理")):
            self.plugin.on_message(self.message("/生图 猫老板征婚图"), self.ctx)
            self.wait_done()
        post.assert_called_once()
        prompt = self.server.calls[0]["arguments"]["prompt"]
        self.assertIn("猫老板征婚图", prompt)
        self.assertIn("以前经营台球厅", prompt)
        self.assertIn("玩宝可梦双打", prompt)
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_planning_timeout_draws_original_request(self):
        with patch.object(self.ctx._llm, "complete_json", side_effect=TimeoutError()) as planner:
            self.plugin.on_message(self.message(), self.ctx)
            self.wait_done()
        planner.assert_called_once()
        self.assertEqual(self.server.calls[0]["arguments"]["prompt"], "水彩橘猫")
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_planning_timeout_with_missing_edit_reference_asks_for_image(self):
        with patch.object(self.ctx._llm, "complete_json", side_effect=TimeoutError()):
            self.plugin.on_message(self.message("/生图 把这张图换个背景"), self.ctx)
            self.wait_done()
        self.assertEqual(self.server.calls, [])
        self.assertIn("需要原图", self.backend.texts[-1][1])

    def test_planning_content_block_does_not_forward_original_prompt(self):
        with patch.object(self.ctx._llm, "complete_json", side_effect=LLMContentBlocked("模型审核拦截")):
            self.plugin.on_message(self.message(), self.ctx)
            self.wait_done()
        self.assertEqual(self.server.calls, [])
        self.assertIn("未提交图片生成", self.backend.texts[-1][1])

    def test_only_requester_can_quote_the_question_to_continue_the_same_task(self):
        with patch.object(self.ctx._llm, "complete_json",
                          side_effect=[{"澄清问题": "图上选哪种动物？"}, {"绘图指令": "绿底水彩狐狸"}]) as model:
            self.plugin.on_message(self.message("/生图 做一张绿底的动物水彩卡"), self.ctx)
            self.wait_done()
            question = next(text for _, text in self.backend.texts if "图上选哪种动物" in text)
            quote = {"quote": {"ref_type": "1", "fromusr": "wxid_robot", "text": question}}
            self.assertFalse(self.plugin.image_reply.followup(self.message("狼", sender="wxid_other", data=quote),
                                                             self.ctx, "狼"))
            self.plugin.on_message(self.message("狐狸", data=quote), self.ctx)
            self.wait_done()
        task = json.loads(model.call_args.args[1])["任务"]
        self.assertEqual(task["本次请求"], "做一张绿底的动物水彩卡")
        self.assertEqual(task["已收到的补充"], ["狐狸"])
        self.assertEqual(self.server.calls[0]["arguments"]["prompt"], "绿底水彩狐狸")

    def test_clear_revision_before_delivery_and_use_new_prompt(self):
        self.server.release.clear()
        with patch.object(self.ctx._llm, "complete_json",
                          side_effect=[{"绘图指令": "绿底橘猫"}, {"绘图指令": "蓝底橘猫"}]):
            original = self.message("/生图 绿底橘猫")
            self.plugin.on_message(original, self.ctx)
            self.assertTrue(self.server.waiting.wait(2))
            self.plugin.on_message(self.message("/生图 正在做的这张把背景改成蓝色"), self.ctx)
            self.server.release.set()
            self.wait_done()
        self.assertEqual([call["arguments"]["prompt"] for call in self.server.calls if call["name"] == "generate_image"],
                         ["绿底橘猫", "蓝底橘猫"])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        receipt = self.members.connection().execute(
            "SELECT source_id,revision FROM reply_receipts WHERE kind='image' AND status='submitted'").fetchone()
        self.assertEqual(tuple(receipt), (self.members.message_id(original), 1))

    def test_chat_question_during_natural_image_request_keeps_original_generation(self):
        mention = {"is_at_self": True, "at_members": ["wxid_robot"]}
        original = self.message("@机器人\u2005生成一张小猫的一天", data=mention)
        question = "三角眼怎么改正？"
        self.server.release.clear()
        self.plugin.on_message(original, self.ctx)
        self.assertTrue(self.server.waiting.wait(2))

        def judge(state, questions, **options):
            if "当前艾特正文" in state:
                return [0.18, 0.01, 0.01]
            if "当前话语" in state:
                return [0.01]
            return [0.99]  # 复现旧版对「改正」的补充误判。

        with (patch.object(self.ctx._jev, "noul", side_effect=judge),
              patch.object(self.ctx._llm, "complete_json", return_value={"动作": "text"}),
              patch.object(self.plugin, "_reply_turn", return_value=iter(())) as chat):
            self.plugin.on_message(self.message("@机器人\u2005" + question, data=mention), self.ctx)
            self.server.release.set()
            self.wait_done()
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[2], question)
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)
        self.assertEqual(self.backend.images, [(ROOM, PNG)])
        receipt = self.members.connection().execute(
            "SELECT source_id,revision FROM reply_receipts WHERE kind='image' AND status='submitted'").fetchone()
        self.assertEqual(tuple(receipt), (self.members.message_id(original), 0))

    def test_only_linked_image_request_can_revise_active_drawing(self):
        mention = {"is_at_self": True, "at_members": ["wxid_robot"]}
        original = self.message("@机器人\u2005画只绿底橘猫", data=mention)
        correction = "刚才那张猫图，背景换成蓝色"
        self.server.release.clear()
        self.plugin.on_message(original, self.ctx)
        self.assertTrue(self.server.waiting.wait(2))

        def judge(state, questions, **options):
            if "当前艾特正文" in state:
                return [0.99, 0.85 if state["当前艾特正文"] == correction else 0.01, 0.01]
            if "正在做的图" in state:
                return [0.99 if state["当前发言"] == correction else 0.01]
            return [0.99] * len(questions)

        with (patch.object(self.ctx._jev, "noul", side_effect=judge),
              patch.object(self.ctx._llm, "complete_json", side_effect=TimeoutError)):
            self.plugin.on_message(self.message("@机器人\u2005另外画张海报", data=mention), self.ctx)
            self.plugin.on_message(self.message("@机器人\u2005" + correction, data=mention), self.ctx)
            self.server.release.set()
            self.wait_done()
        prompts = [c["arguments"]["prompt"] for c in self.server.calls if c["name"] == "generate_image"]
        self.assertEqual(prompts, ["水彩绘制一只橘猫，浅色背景", "画只绿底橘猫\n" + correction])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_explicit_edit_delivers_first_result_without_review_or_redraw(self):
        reference = self.poster_fixture()
        self.server.image = PNG
        with patch.object(self.ctx._llm, "complete_json", return_value={
            "绘图指令": "换成蓝底，保留人物", "改图": True, "检查项": ["人物保持原样"]
        }):
            self.plugin.on_message(self.message("/生图 换个背景，保留人物",
                data={"quote": {"ref_type": "3", "svrid": "912"}}), self.ctx)
            self.wait_done()
        self.assertEqual([call["name"] for call in self.server.calls], ["generate_image", "generation_result"])
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(reference.resolve())])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_model_can_recover_an_earlier_sent_picture_from_its_original_request(self):
        past = self.message("/生图 画一张山谷跑团封面")
        picture = self.root / "valley.png"
        picture.write_bytes(PNG)
        self.members.record_sent_image(ROOM, str(picture), "test-digest", self.members.message_id(past))
        with patch.object(self.ctx._llm, "complete_json", side_effect=[
            lookup("find_images", keyword="跑团"), {"绘图指令": "沿用山谷构图，改成雪景", "参考图": "I1"}
        ]):
            self.plugin.on_message(self.message("/生图 沿用上次跑团构图，改成雪景"), self.ctx)
            self.wait_done()
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(picture)])

    def test_plan_can_follow_its_original_reply_into_relevant_group_history(self):
        self.message("灯会路线旧版走西门", sender="wxid_festival")
        self.members.record_bot_reply(ROOM, "灯会路线旧版建议走西门")
        self.members.clear(ROOM)
        self.members.register("阿青", ROOM, "wxid_festival")
        self.message("灯会走北门，经过石桥，再到灯廊", sender="wxid_festival")
        self.members.record_bot_reply(ROOM, "灯会路线可以画成三站地图，具体地点按阿青最终确认的为准")
        for i in range(10):
            self.message(f"其他话题第{i}条", sender="wxid_other")
        request = self.message("/生图 按你以前建议的画法，把那晚群里商量的灯会路线画出来")
        self.message("灯会临时改走南门", sender="wxid_festival")
        outputs = [
            lookup("search_messages", keywords=["灯会"], speaker="bot"),
            lookup("search_messages", keywords=["灯会"], speaker="member"),
            {"绘图指令": "绘制三站灯会路线图，按顺序标注北门、石桥、灯廊"},
        ]
        with patch.object(self.ctx._llm, "complete_json", side_effect=outputs) as planner:
            self.plugin.on_message(request, self.ctx)
            self.wait_done()
        payload = json.loads(planner.call_args.args[1])
        old, group = [batch[0]["结果"]["消息"] for batch in payload["取材记录"]]
        self.assertEqual([row["内容"] for row in old],
                         ["灯会路线可以画成三站地图，具体地点按阿青最终确认的为准"])
        self.assertEqual([row["发言人"] for row in group], ["阿青"])
        self.assertEqual([row["内容"] for row in group], ["灯会走北门，经过石桥，再到灯廊"])
        self.assertEqual(self.server.calls[0]["arguments"]["prompt"], "绘制三站灯会路线图，按顺序标注北门、石桥、灯廊")

    def test_missing_reference_and_self_message_do_not_submit(self):
        self.plugin.on_message(self.message("/生图 修改这张图", data={"quote": {"ref_type": "3", "svrid": "missing"}}), self.ctx)
        self.plugin.on_message(replace(self.message(), is_self=True), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls, [])
        self.assertTrue(any("引用的原图" in text for _, text in self.backend.texts))

    def test_new_image_is_not_forced_to_use_candidate_from_unrelated_recent_upload(self):
        path = self.root / "unrelated.png"
        path.write_bytes(PNG)
        RECENT_IMAGES.note({"chat_id": ROOM, "sender_id": "wxid_member", "native_message_id": "901",
                            "image_status": "ready", "image_path": str(path)})
        self.plugin.on_message(self.message("/生图 画一只橘猫，这张图用水彩"), self.ctx)
        self.wait_done()
        arguments = self.server.calls[0]["arguments"]
        self.assertNotIn("reference_images", arguments)

    def test_clear_during_generation_prevents_delivery(self):
        self.server.release.clear()
        self.plugin.on_message(self.message(), self.ctx)
        self.assertTrue(self.server.submitted.wait(2))
        self.members.clear(ROOM)
        self.server.release.set()
        self.wait_done()
        self.assertEqual(self.backend.images, [])

    def test_queue_capacity_and_shutdown_prevent_extra_submissions(self):
        self.server.release.clear()
        for n in range(4):
            self.plugin.on_message(self.message(sender=f"wxid_member{n}"), self.ctx)
        self.assertTrue(self.server.submitted.wait(2))
        self.wait_for(lambda: any("队列满" in text for _, text in self.backend.texts))
        self.plugin.image_reply.close()
        self.assertEqual(self.backend.images, [])
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)

    def test_send_failure_does_not_generate_again(self):
        self.backend.image_success = False
        self.plugin.on_message(self.message(), self.ctx)
        self.wait_done()
        self.assertTrue(any("提交结果未确认" in text for _, text in self.backend.texts))
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)
