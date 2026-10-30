"""好友/群消息路由和实际桥接 ABI 的回归验证，不向微信发送消息。"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import time
import unittest

from wxbot_framework.sdk import Message
from wxbot_framework.messaging.text import TextRouter, validate_target

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native"
SELF = "wxid_framework_self"
FRIEND = "wxid_framework_friend"
GROUP = "12345678901234567890@chatroom"


class TextRoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = TextRouter()

    def identify(self):
        return self.router.convert(SELF, "filehelper", "init")

    def test_private_chat_requires_account_and_keeps_actual_sender(self):
        self.assertIsNone(self.router.convert(FRIEND, SELF, "未识别"))
        self.assertEqual(self.router.unidentified, 1)
        self.identify()
        data = self.router.convert(FRIEND, SELF, "echo 好友 🌸\n第二行", sequence=3)
        message = Message.from_native(11046, data, "session")
        self.assertEqual((message.chat_id, message.sender_id, message.receiver_id), (FRIEND, FRIEND, SELF))
        self.assertFalse(message.is_group)
        self.assertFalse(message.is_self)
        self.assertEqual(message.text, "echo 好友 🌸\n第二行")

    def test_group_identifies_account_and_separates_author_body_and_chat(self):
        data = self.router.convert(GROUP, SELF, FRIEND + ":\necho 群聊 🌸\n二:\n三", sequence=9)
        message = Message.from_native(11046, data, "session")
        self.assertEqual(self.router.self_wxid, SELF)
        self.assertEqual((message.chat_id, message.sender_id), (GROUP, FRIEND))
        self.assertEqual(message.text, "echo 群聊 🌸\n二:\n三")
        self.assertTrue(message.is_group)
        self.assertEqual(data["from_wxid"], FRIEND)
        self.assertEqual(data["room_wxid"], GROUP)

    def test_phone_sent_private_and_group_messages_do_not_create_reply_loops(self):
        self.identify()
        self.assertIsNone(self.router.convert(SELF, FRIEND, "echo 私聊同步"))
        self.assertIsNone(self.router.convert(SELF, GROUP, "echo 群同步"))
        self.assertIsNone(self.router.convert(GROUP, SELF, SELF + ":\necho 自己的群消息"))
        self.assertEqual(self.router.ignored_self, 3)
        # 文件传输助手仍允许手机自己发送测试指令。
        self.assertTrue(self.identify()["is_self"])

    def test_unrecognized_group_author_is_not_guessed(self):
        self.assertIsNone(self.router.convert(GROUP, SELF, "没有成员前缀"))
        self.assertIsNone(self.router.convert(GROUP, SELF, "普通内容:\n正文"))
        self.assertEqual(self.router.unparsed_groups, 2)

    def test_primary_phone_can_control_and_play_group_game(self):
        self.identify()
        for text in ("/help", "成语接龙", "合并接龙", "退出接龙", "意气风发",
                     "接龙提示", "接龙换题", "接龙状态", "接龙认输", "/接龙 意气风发"):
            data = self.router.convert(GROUP, SELF, SELF + ":\n" + text)
            self.assertTrue(data["is_self"])
            self.assertEqual(data["msg"], text)

    def test_native_service_skips_recent_bot_text_but_allows_players(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMessage, ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(SELF)
        service._sent_texts.append((GROUP, "意气风发", time.monotonic()))
        own = ReceivedMessage(type=1, sender=GROUP.encode(), receiver=SELF.encode(),
                              text=(SELF + ":\n意气风发").encode("utf-8"))
        mapped = service._map_message(own)
        self.assertTrue(mapped["bot_generated"])
        self.assertFalse(mapped["dispatch_to_plugins"])
        own.text = (SELF + ":\n发扬光大").encode("utf-8")
        self.assertEqual(service._map_message(own)["msg"], "发扬光大")
        own.text = (FRIEND + ":\n意气风发").encode("utf-8")
        self.assertEqual(service._map_message(own)["sender_id"], FRIEND)

    def test_account_change_stops_old_connection(self):
        self.identify()
        with self.assertRaises(RuntimeError):
            self.router.convert("wxid_another_account", "filehelper", "init")
        self.assertEqual(self.router.self_wxid, SELF)

    def test_public_and_system_peers_are_not_dispatched(self):
        self.identify()
        self.assertIsNone(self.router.convert("gh_example", SELF, "echo 公众号"))
        self.assertIsNone(self.router.convert("weixin", SELF, "echo 系统"))

    def test_target_validation_matches_native_buffer(self):
        for target in ("filehelper", FRIEND, GROUP, "a" * 127):
            self.assertEqual(validate_target(target), target)
        for target in ("", "a" * 128, "好友名称", "wxid_a\0b", "a b", None):
            with self.assertRaises(ValueError):
                validate_target(target)

    @unittest.skipUnless(sys.platform == "win32", "本机 Windows ABI 检查")
    def test_compiled_bridge_exports_and_wire_structures(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        import pefile
        from run_selftest import Config, TextConfig, AtTextConfig, ImageDownloadConfig, TEXT_DLL
        from filehelper_service import (ReceivedMessage, ReceivedTextMessage, ReceivedTextMessageV2,
                                        ReceivedMediaMessageV4)
        from text_service import V4_EXPORT, V3_EXPORT, V2_EXPORT, text_message_binding
        self.assertEqual(ctypes.sizeof(Config), 1560)
        self.assertEqual(ctypes.sizeof(TextConfig), 1688)
        self.assertEqual(TextConfig.target.offset, 1560)
        self.assertEqual(ctypes.sizeof(AtTextConfig), 2712)
        self.assertEqual(AtTextConfig.members.offset, 1688)
        self.assertEqual(ctypes.sizeof(ImageDownloadConfig), 2656)
        self.assertEqual(ImageDownloadConfig.filekey.offset, 1560)
        self.assertEqual(ImageDownloadConfig.aeskey.offset, 2584)
        self.assertEqual(ImageDownloadConfig.image_type.offset, 2652)
        self.assertEqual(ctypes.sizeof(ReceivedMessage), 1312)
        self.assertEqual(ctypes.sizeof(ReceivedTextMessageV2), 5408)  # 正文 1KB + 艾特来源
        self.assertEqual(ctypes.sizeof(ReceivedTextMessage), 12576)  # 正文缓冲 8KB + 艾特来源
        self.assertEqual(ctypes.sizeof(ReceivedMediaMessageV4), 28968)  # + 图片 XML 16KB + 原始编号
        self.assertEqual(ReceivedMediaMessageV4.image_xml.offset, 12576)
        self.assertEqual(ReceivedMediaMessageV4.native_message_id.offset, 28960)
        with pefile.PE(str(TEXT_DLL)) as pe:
            exports = {item.name for item in pe.DIRECTORY_ENTRY_EXPORT.symbols}
            self.assertEqual(pe.FILE_HEADER.Machine, 0x8664)
            self.assertTrue({b"WxSendText", b"WxSendAtText", b"WxStartTextReceiver", b"WxPollTextMessage",
                             b"WxPollTextMessageV2", b"WxWaitForTasks", b"WxRemoveSelfTest"}.issubset(exports))
        # 接收协议跟着已编译 DLL 走：V4 收图片，V3 收 8KB 正文，否则按 V2 的 1KB 正文收，都能正常启动。
        poll_export, message_type = text_message_binding(exports)
        if V4_EXPORT in exports:
            self.assertEqual((poll_export, message_type), (V4_EXPORT, ReceivedMediaMessageV4))
        elif V3_EXPORT in exports:
            self.assertEqual((poll_export, message_type), (V3_EXPORT, ReceivedTextMessage))
        else:
            self.assertEqual((poll_export, message_type), (V2_EXPORT, ReceivedTextMessageV2))
        # 源码已声明 V4（重编桥接后即启用）；这里防止有人只改一边。
        source = (NATIVE / "text_bridge.cpp").read_text(encoding="utf-8")
        self.assertIn("WxPollMediaMessageV4", source)
        self.assertIn("WxPollTextMessageV3", source)
        self.assertIn("WxPollTextMessageV2", source)
        self.assertIn('#include "image_download.h"', source)
        self.assertIn("WxDownloadImage", (NATIVE / "image_download.h").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
