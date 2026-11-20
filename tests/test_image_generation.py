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

from support.recyclable_tempdir import RecyclableTemporaryDirectory
from plugins.mention_chat import Plugin
from wxbot_framework.conversation.members import MemberStore
from wxbot_framework.conversation.status_replies import StatusReplies
from wxbot_framework.integrations.jev import JevError
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.integrations.magpie_images import MagpieImages
from wxbot_framework.integrations.magpie_tools import MagpieTools, MagpieToolsToolError
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
        if "拟发送回复" in state:
            return [0.99]
        return [0.99, 0.01]


class StatusModel:
    def __init__(self):
        self.calls = []

    def complete_text(self, system, user, **options):
        payload, _ = json.JSONDecoder().raw_decode(user)
        self.calls.append(payload)
        return "，".join(payload["实际状态"].values())


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
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.ctx = PluginContext("mention_chat", {}, self.service,
                                 {"group.image.send", "group.image.receive", "group.text.mention"},
                                 member_store=self.members)
        self.ctx._magpie, self.ctx._jev = self.magpie, Jev()
        self.ctx._llm = StatusModel()
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

    def test_quoted_image_is_forwarded_as_exact_local_reference(self):
        reference = self.root / "reference.png"
        reference.write_bytes(PNG)
        RECENT_IMAGES.note({"chat_id": ROOM, "sender_id": "wxid_member", "native_message_id": "900",
                            "image_status": "ready", "image_path": str(reference)})
        self.plugin.on_message(self.message("/生图 把背景改成雪山", data={"quote": {"ref_type": "3", "svrid": "900"}}), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls[0]["arguments"]["reference_images"], [str(reference.resolve())])
        self.assertEqual(self.backend.images, [(ROOM, PNG)])

    def test_missing_reference_and_self_message_do_not_submit(self):
        self.plugin.on_message(self.message("/生图 修改这张图"), self.ctx)
        self.plugin.on_message(replace(self.message(), is_self=True), self.ctx)
        self.wait_done()
        self.assertEqual(self.server.calls, [])
        self.assertIn("引用目标图片", self.backend.texts[0][1])

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
