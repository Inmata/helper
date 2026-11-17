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
        return [0.99, 0.01]


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = RecyclableTemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls, self.server.image, self.server.fail = [], PNG, False
        self.server.errors = {}
        self.server.submitted, self.server.release = threading.Event(), threading.Event()
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
        self.plugin = Plugin()
        self.plugin.members, self.plugin.room_state = self.members, None
        self.output_patch = patch("plugins.mention_chat.image_reply.OUTPUT_DIR", self.root / "output")
        self.output_patch.start()
        self.plugin.image_reply.start(self.ctx)
        self.sequence = 0
        RECENT_IMAGES.clear()

    def tearDown(self):
        self.server.release.set()
        self.plugin.image_reply.close()
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
        deadline = time.monotonic() + 5
        while self.plugin.has_pending_callbacks() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.plugin.has_pending_callbacks())

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
        self.assertEqual(self.server.calls, [])
        self.assertIn("请引用", self.backend.texts[0][1])

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
        self.assertTrue(any("队列满" in text for _, text in self.backend.texts))
        self.plugin.image_reply.close()
        self.assertEqual(self.backend.images, [])
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)

    def test_send_failure_does_not_generate_again(self):
        self.backend.image_success = False
        self.plugin.on_message(self.message(), self.ctx)
        self.wait_done()
        self.assertTrue(any("图片没发成功" in text for _, text in self.backend.texts))
        self.assertEqual(sum(c["name"] == "generate_image" for c in self.server.calls), 1)
