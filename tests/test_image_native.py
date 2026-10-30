"""原生图片接收接口、消息类型和下载参数的离线回归；不注入 DLL、不访问微信。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support.recyclable_tempdir import RecyclableTemporaryDirectory
from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.image_receiver import ImageDownloadSkipped, ImageReceiver
from wxbot_framework.integrations.wechat import (CAPABILITIES, IMAGE_CAPABILITIES, TEXT_CAPABILITIES,
                                                 TEXT_CAPABILITIES_BASE, NativeTextBackend,
                                                 compiled_bridge_exports, text_capabilities, text_scope)
from wxbot_framework.messaging.message import Message

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native"
SELF = "wxid_image_self"
FRIEND = "wxid_image_friend"
GROUP = "12345678901234567890@chatroom"
AESKEY = "0123456789abcdef0123456789abcdef"
XML = (f'<msg><img aeskey="{AESKEY}" cdnmidimgurl="https://cdn/mid" '
       'filekey="mid-key" length="1024" md5="fedcba9876543210fedcba9876543210" /></msg>')


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False



def native_imports():
    if str(NATIVE) not in sys.path:
        sys.path.insert(0, str(NATIVE))


class MessageTypeTests(unittest.TestCase):
    def test_image_event_has_type_three_empty_text_and_session(self):
        data = {"wx_type": 3, "msg": XML, "content": XML, "room_wxid": GROUP,
                "chat_id": GROUP, "sender_id": FRIEND, "sequence": 8,
                "image_status": "ready", "image_path": "C:/images/a.png"}
        message = Message.from_native(11046, data, "session-image")
        self.assertEqual(message.type, 3)
        self.assertTrue(message.is_image)
        self.assertEqual(message.text, "")
        self.assertEqual(message.session_id, "session-image")
        self.assertEqual(message.data["session_id"], "session-image")
        self.assertEqual(message.image_status, "ready")
        self.assertEqual(message.image_path, "C:/images/a.png")
        self.assertEqual(message.sequence, 8)

    def test_text_event_keeps_body_and_defaults_to_type_one(self):
        message = Message.from_native(11046, {"msg": "你好", "chat_id": FRIEND}, "session-text")
        self.assertEqual((message.type, message.text), (1, "你好"))
        self.assertFalse(message.is_image)
        self.assertEqual(message.data["session_id"], "session-text")

    def test_unknown_wire_type_falls_back_to_text(self):
        for value in (None, "image", -1, 2 ** 40):
            message = Message.from_native(11046, {"type": value, "msg": "正文"}, "s")
            self.assertEqual((message.type, message.text), (1, "正文"))


class CapabilityTests(unittest.TestCase):
    V4 = b"WxPollMediaMessageV4"
    DOWNLOAD = b"WxDownloadImage"
    V3 = b"WxPollTextMessageV3"

    def test_image_capabilities_require_media_queue_and_download_export(self):
        self.assertNotIn("image.receive", CAPABILITIES)
        self.assertTrue(IMAGE_CAPABILITIES.issubset(text_capabilities({self.V4, self.DOWNLOAD})))
        self.assertFalse(IMAGE_CAPABILITIES & text_capabilities({self.V3, b"WxSendText"}))
        # 只有其一也不声明：V4 没有下载接口，或有下载接口但没有 V4 媒体队列。
        self.assertFalse(IMAGE_CAPABILITIES & text_capabilities({self.V4}))
        self.assertFalse(IMAGE_CAPABILITIES & text_capabilities({self.DOWNLOAD}))
        self.assertEqual(text_capabilities({self.V3}), TEXT_CAPABILITIES_BASE)

    def test_effective_capabilities_follow_compiled_dll(self):
        exports = compiled_bridge_exports()
        self.assertEqual(TEXT_CAPABILITIES, text_capabilities(exports))
        if {self.V4, self.DOWNLOAD}.issubset(exports):
            self.assertTrue(IMAGE_CAPABILITIES.issubset(TEXT_CAPABILITIES))
        else:
            # 当前编译的是 V3 DLL：不声明图片能力，依赖它的插件不会启用。
            self.assertFalse(IMAGE_CAPABILITIES & TEXT_CAPABILITIES)

    def test_scope_follows_declared_image_capability(self):
        # 连接日志展示的范围必须与实际能力一致：V3 DLL 不该声称能收群图片。
        self.assertNotIn("群图片", text_scope(TEXT_CAPABILITIES_BASE))
        self.assertIn("群图片", text_scope(TEXT_CAPABILITIES_BASE | IMAGE_CAPABILITIES))
        self.assertEqual("群图片" in text_scope(), "image.receive" in TEXT_CAPABILITIES)

    def test_backend_passes_image_config_and_reports_counter(self):
        with RecyclableTemporaryDirectory(prefix="wxbot-image-backend-") as directory:
            backend = NativeTextBackend(ROOT, {"image_directory": directory,
                                               "image_allowlist": [GROUP]})
            self.assertEqual(backend.raw.media_directory, Path(directory))
            self.assertEqual(backend.raw.image_allowlist, frozenset({GROUP}))
            self.assertEqual(backend.diagnostics()["image_messages"], 0)
            self.assertEqual(backend.scope, text_scope())


class NativeImageMappingTests(unittest.TestCase):
    def setUp(self):
        native_imports()
        from filehelper_service import ReceivedMediaMessageV4
        from text_service import NativeTextService
        self.ReceivedMediaMessageV4 = ReceivedMediaMessageV4
        self.service = NativeTextService(pid=123, image_allowlist=())
        self.service.router.set_account(SELF)

    def tearDown(self):
        receiver = self.service._image_receiver
        if receiver is not None and hasattr(receiver, "close"):
            receiver.close(timeout=1)

    def image_message(self, message_id=99, sender=FRIEND, receiver=SELF, body=None):
        message = self.ReceivedMediaMessageV4(
            type=3, sender=sender.encode(), receiver=receiver.encode(),
            image_xml=(body or XML).encode("utf-8"), sequence=7, native_message_id=message_id)
        return message

    def test_group_image_is_mapped_to_async_event(self):
        captured = []

        class Recorder:
            def submit(self, data):
                captured.append(dict(data))
                return True

        self.service._image_receiver = Recorder()
        body = FRIEND + ":\n" + XML
        self.assertIsNone(self.service._map_message(
            self.image_message(sender=GROUP, receiver=SELF, body=body)))
        self.assertEqual(self.service.image_messages, 1)
        self.assertEqual(len(captured), 1)
        event = captured[0]
        self.assertEqual(event["wx_type"], 3)
        self.assertEqual((event["msg"], event["content"]), ("", ""))
        # raw_content 是交给接收器的内部字段；接收器解析后立即移除，不进入插件事件。
        self.assertEqual(event["raw_content"], XML)
        self.assertEqual(event["image_status"], "pending")
        self.assertEqual(event["chat_id"], GROUP)
        self.assertEqual(event["sender_id"], FRIEND)
        self.assertEqual(event["sequence"], 7)
        self.assertEqual(event["native_message_id"], "99")

    def test_private_image_is_mapped_and_allowed(self):
        captured = []

        class Recorder:
            def submit(self, data):
                captured.append(dict(data))
                return True

        self.service._image_receiver = Recorder()
        message = self.image_message(sender=FRIEND, receiver=SELF, body=XML)
        self.assertIsNone(self.service._map_message(message))
        self.assertEqual(captured[0]["chat_id"], FRIEND)
        self.assertEqual(captured[0]["raw_content"], XML)

    def test_delivered_image_event_has_no_xml_url_or_keys(self):
        with RecyclableTemporaryDirectory(prefix="wxbot-native-image-") as directory:
            store = ImageStore(Path(directory) / "images", SELF)
            events = []

            def download(_metadata, destination):
                Path(destination).write_bytes(b"\x89PNG\r\n\x1a\nnative")
                return True

            receiver = ImageReceiver(store, download, events.append, lambda: True)
            self.service._image_receiver = receiver
            body = FRIEND + ":\n" + XML
            self.assertIsNone(self.service._map_message(
                self.image_message(sender=GROUP, receiver=SELF, body=body)))
            self.assertTrue(wait_until(lambda: events))
            event = events[0]
            self.assertEqual(event["image_status"], "ready")
            self.assertTrue(Path(event["image_path"]).is_file())
            self.assertNotIn("raw_content", event)
            rendered = repr(event)
            for secret in (AESKEY, "mid-key", "https://cdn/mid", "<msg", "<img"):
                self.assertNotIn(secret, rendered)

    def test_image_allowlist_delivers_skipped_without_download(self):
        # 规范要求会话不在图片名单时投递 skipped：不下载，但插件能观察到原因。
        self.service.image_allowlist = frozenset({GROUP})
        other_room = "98765432109876543210@chatroom"
        message = self.image_message(sender=other_room, receiver=SELF,
                                     body=FRIEND + ":\n" + XML)
        data = self.service._map_message(message)
        self.assertIsNotNone(data)
        self.assertEqual(data["image_status"], "skipped")
        self.assertTrue(data["image_error"])
        self.assertIn("名单", data["image_error"])
        for field in ("raw_content", "msg", "content"):
            self.assertEqual(data[field], "")
        # 未创建接收器，也没有发起下载。
        self.assertIsNone(self.service._image_receiver)
        rendered = repr(data)
        for secret in (AESKEY, "mid-key", "https://cdn/mid", "<msg", "<img"):
            self.assertNotIn(secret, rendered)
        delivery = Message.from_native(11046, data, "session-skipped")
        self.assertEqual((delivery.type, delivery.text), (3, ""))
        self.assertEqual(delivery.image_status, "skipped")
        self.assertEqual(delivery.image_path, "")
        self.assertEqual(delivery.data.get("raw_content", ""), "")

    def test_missing_receiver_yields_observable_failure_without_xml(self):
        self.service._ensure_image_receiver = lambda: None
        body = FRIEND + ":\n" + XML
        data = self.service._map_message(self.image_message(body=body))
        self.assertEqual(data["image_status"], "failed")
        self.assertTrue(data["image_error"])
        for field in ("raw_content", "msg", "content"):
            self.assertEqual(data[field], "")


class ImageDownloadMappingTests(unittest.TestCase):
    """核对 XML 元数据到原生下载参数的映射；不调用 DLL 或微信。"""

    MID = ('<msg><img aeskey="0123456789abcdef0123456789abcdef" '
           'cdnmidimgurl="https://cdn/mid" filekey="mid-key" /></msg>')
    BIG = ('<msg><img aeskey="0123456789abcdef0123456789abcdef" '
           'cdnbigimgurl="https://cdn/big" /></msg>')
    EMPTY = '<msg><img md5="0123456789abcdef0123456789abcdef" /></msg>'
    THUMB = ('<msg><img cdnthumburl="https://cdn/thumb" '
             'cdnthumbaeskey="0123456789abcdef" cdnthumblength="120" /></msg>')
    SHORT_KEY = ('<msg><img aeskey="0123456789abcdef" '
                 'cdnmidimgurl="https://cdn/mid" /></msg>')

    def setUp(self):
        native_imports()
        from wxbot_framework.image_metadata import parse_image_metadata
        from text_service import NativeTextService
        self.parse = parse_image_metadata
        self.service = NativeTextService(pid=123)
        self.service._owns_bridge = True
        self.service._check_account = lambda **_options: None
        self.calls = []
        self.service._run = self.capture

    def capture(self, mode, **options):
        self.calls.append((mode, options))
        return {"success": True}

    def download(self, content):
        return self.service._download_image(self.parse(content), Path("C:/images/x.part"))

    def test_mid_image_uses_cdn_url_and_full_aes_key(self):
        self.assertTrue(self.download(self.MID))
        mode, options = self.calls[0]
        self.assertEqual(mode, "--download-image")
        self.assertEqual(options["download_image"], {
            "destination": str(Path("C:/images/x.part")),
            "filekey": "https://cdn/mid",
            "aeskey": "0123456789abcdef0123456789abcdef",
            "image_type": 2})

    def test_big_image_uses_big_url_and_type_one(self):
        self.assertTrue(self.download(self.BIG))
        download = self.calls[0][1]["download_image"]
        self.assertEqual((download["filekey"], download["image_type"]),
                         ("https://cdn/big", 1))

    def test_missing_download_keys_is_skipped_without_native_call(self):
        with self.assertRaises(ImageDownloadSkipped) as caught:
            self.download(self.EMPTY)
        self.assertIn("没有可下载地址", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_thumbnail_only_image_is_skipped_with_reason(self):
        # 原生接口只接受 32/64 位十六进制密钥；缩略图只有 16 位 cdnthumbaeskey，
        # 上游同样没有可用的转换依据，因此明确 skipped 而不是含糊 failed。
        with self.assertRaises(ImageDownloadSkipped) as caught:
            self.download(self.THUMB)
        self.assertIn("缩略图", str(caught.exception))
        self.assertIn("不受原生下载接口支持", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_incompatible_key_is_skipped_without_native_call(self):
        with self.assertRaises(ImageDownloadSkipped) as caught:
            self.download(self.SHORT_KEY)
        self.assertIn("密钥格式", str(caught.exception))
        self.assertEqual(self.calls, [])


class NativeWireTests(unittest.TestCase):
    def setUp(self):
        native_imports()

    def test_binding_prefers_compiled_media_export(self):
        from filehelper_service import ReceivedMediaMessageV4, ReceivedTextMessage, ReceivedTextMessageV2
        from text_service import V2_EXPORT, V3_EXPORT, V4_EXPORT, text_message_binding
        self.assertEqual(text_message_binding({V4_EXPORT}), (V4_EXPORT, ReceivedMediaMessageV4))
        self.assertEqual(text_message_binding({V3_EXPORT}), (V3_EXPORT, ReceivedTextMessage))
        self.assertEqual(text_message_binding({V2_EXPORT}), (V2_EXPORT, ReceivedTextMessageV2))
        self.assertEqual(text_message_binding(set()), (V2_EXPORT, ReceivedTextMessageV2))

    def test_download_parameter_validation(self):
        from run_selftest import _validate_download_image
        with RecyclableTemporaryDirectory(prefix="wxbot-download-param-") as directory:
            destination = str(Path(directory) / "target.bin")
            valid = {"destination": destination, "filekey": "a" * 32,
                     "aeskey": "0123456789abcdef0123456789abcdef", "image_type": 2}
            self.assertEqual(_validate_download_image(valid),
                             (destination, "a" * 32, "0123456789abcdef0123456789abcdef", 2))
            Path(destination).write_bytes(b"x")
            for field, value in (("destination", destination),
                                 ("destination", "relative.bin"),
                                 ("destination", str(Path(directory) / "missing" / "x.bin")),
                                 ("filekey", ""), ("filekey", "中文"),
                                 ("aeskey", "0" * 31), ("aeskey", "g" * 32),
                                 ("aeskey", "0123456789abcdef"),
                                 ("image_type", 0), ("image_type", 4), ("image_type", True)):
                broken = dict(valid, **{field: value})
                if field == "destination" and value == destination:
                    Path(destination).write_bytes(b"x")
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        _validate_download_image(broken)

    def test_download_export_is_declared_in_source(self):
        source = (NATIVE / "image_download.h").read_text(encoding="utf-8")
        self.assertIn("WxDownloadImage", source)
        self.assertNotIn("DeleteFileW", source)
        self.assertIn("MoveFileExW", source)
        # 跨卷移动会复制后删除源文件；这里只允许同卷改名，跨卷改为复制并保留缓存。
        self.assertNotIn("MOVEFILE_COPY_ALLOWED", source)
        self.assertIn("CopyFileW", source)


class DownloadRunEntryTests(unittest.TestCase):
    """经过 run() 入口的离线替身：确认 --download-image 选中正确的原生导出。

    上一轮测试替换了 ``_run``，没有覆盖 run() 内部的导出映射；V3/V2 的旧映射
    缺少 ``--download-image``，会先抛 KeyError，插件只能收到 failed。
    """

    UUID_HEX = "0" * 32
    EXPORTS = {b"WxDownloadImage": 0x100, b"WxWaitForTasks": 0x200,
               b"WxRemoveSelfTest": 0x300, b"WxSendText": 0x400}

    @unittest.skipUnless(sys.platform == "win32", "本机 Windows ABI 检查")
    def test_download_image_mode_uses_image_export(self):
        native_imports()
        import run_selftest
        with RecyclableTemporaryDirectory(prefix="wxbot-run-entry-") as directory:
            root = Path(directory)
            (root / "native").mkdir()
            (root / "native/compatibility.json").write_text(
                json.dumps({"sha256": "f" * 64}), encoding="utf-8")
            core = root / "weixin.dll"
            core.write_bytes(b"core")
            destination = root / "target.bin"
            report_directory = root / ".runtime/native_requests"
            report_directory.mkdir(parents=True)
            report = {"completion_success": True, "operation": "image_download",
                      "image_path": str(destination), "bytes_written": 5}
            (report_directory / (self.UUID_HEX + ".json")).write_text(
                json.dumps(report), encoding="utf-8")
            loaded = {"weixin.dll": (0x10000000, str(core)),
                      "text_bridge.dll": (0x20000000, str(run_selftest.TEXT_DLL))}
            addresses = []

            class FakePE:
                VS_FIXEDFILEINFO = [SimpleNamespace(FileVersionMS=0x40001, FileVersionLS=0xF000D)]
                DIRECTORY_ENTRY_EXPORT = SimpleNamespace(
                    symbols=[SimpleNamespace(name=name, address=address)
                             for name, address in DownloadRunEntryTests.EXPORTS.items()])

            def query(process, flags, path, length):
                path.value = r"C:\Weixin\Weixin.exe"
                return True

            def write(process, address, buffer, size, written):
                written._obj.value = size
                return True

            def remote(process, address, parameter, timeout=30000):
                addresses.append(address)
                return 0

            fake_kernel = SimpleNamespace(
                OpenProcess=lambda *args: 1, CloseHandle=lambda handle: True,
                QueryFullProcessImageNameW=query,
                VirtualAllocEx=lambda *args: 0x50000000,
                VirtualFreeEx=lambda *args: True,
                WriteProcessMemory=write)
            with mock.patch.object(run_selftest, "ROOT", root), \
                    mock.patch.object(run_selftest, "modules", lambda pid: dict(loaded)), \
                    mock.patch.object(run_selftest, "remote_call", remote), \
                    mock.patch.object(run_selftest, "K", fake_kernel), \
                    mock.patch.object(run_selftest, "pefile") as pefile_module, \
                    mock.patch.object(run_selftest.uuid, "uuid4",
                                      lambda: SimpleNamespace(hex=self.UUID_HEX)), \
                    mock.patch.object(run_selftest.hashlib, "sha256") as sha256:
                pefile_module.PE.side_effect = lambda path: FakePE()
                sha256.return_value.hexdigest.return_value = "f" * 64
                result = run_selftest.run(
                    "--download-image", pid=1, keep_loaded=True,
                    bridge_path=run_selftest.TEXT_DLL,
                    download_image={"destination": str(destination), "filekey": "k" * 32,
                                    "aeskey": "a" * 32, "image_type": 2})
            self.assertEqual(result["mode"], "--download-image")
            self.assertEqual(addresses[0], 0x20000000 + self.EXPORTS[b"WxDownloadImage"])
            self.assertEqual(addresses[1], 0x20000000 + self.EXPORTS[b"WxWaitForTasks"])
            self.assertTrue(result["success"])
            self.assertEqual(result["report"]["image_path"], str(destination))


if __name__ == "__main__":
    unittest.main()
