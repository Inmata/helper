"""异步图片接收队列的离线回归；下载器为测试替身，不连接微信 CDN。"""
from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest

from tests.support.recyclable_tempdir import RecyclableTemporaryDirectory
from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.image_receiver import ImageDownloadSkipped, ImageReceiver


PNG = b"\x89PNG\r\n\x1a\n" + b"receiver-test"
AESKEY = "8399c0d5e5f4b2a1c3d4e5f60718293a"
MD5 = "0123456789abcdef0123456789abcdef"
XML = (f'<msg><img aeskey="{AESKEY}" cdnmidimgurl="https://cdn/mid" '
       f'filekey="mid-key" length="1024" md5="{MD5}" /></msg>')
EVENT = {"wx_type": 3, "chat_id": "1@chatroom", "room_wxid": "1@chatroom", "sender_id": "wxid_a",
         "sequence": 4, "native_message_id": "99", "raw_content": XML, "image_status": "pending",
         "image_path": "", "image_error": ""}
SECRETS = (AESKEY, "mid-key", "https://cdn/mid", "<msg", "<img")



def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ImageReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = RecyclableTemporaryDirectory(prefix="wxbot-image-receiver-")
        self.root = Path(self.temporary.name) / "images"
        self.store = ImageStore(self.root, "wxid_receiver")
        self.events = []
        self.events_lock = threading.Lock()
        self.receivers = []

    def tearDown(self):
        for receiver in self.receivers:
            receiver.close(timeout=2)
        self.temporary.cleanup()

    def make(self, downloader, deliver=None, is_current=lambda: True, queue_size=4):
        receiver = ImageReceiver(self.store, downloader,
                                 deliver or self.record, is_current, queue_size=queue_size)
        self.receivers.append(receiver)
        return receiver

    def record(self, data):
        with self.events_lock:
            self.events.append(dict(data))

    def write_png(self, _metadata, destination):
        Path(destination).write_bytes(PNG)
        return True

    def event_ids(self):
        with self.events_lock:
            return [event.get("native_message_id") for event in self.events]

    def assert_public(self, event):
        """插件只应看到公开元数据、状态和本地路径；XML、CDN 地址与密钥都不能出现。"""
        self.assertNotIn("raw_content", event)
        rendered = repr(event)
        for secret in SECRETS:
            self.assertNotIn(secret, rendered)
        for name in ("aeskey", "cdnthumbaeskey", "filekey", "cdnthumburl", "cdnmidimgurl", "cdnbigimgurl"):
            self.assertNotIn(name, rendered)

    def test_valid_image_is_downloaded_committed_and_delivered(self):
        receiver = self.make(self.write_png)
        self.assertTrue(receiver.submit(dict(EVENT)))
        self.assertTrue(wait_until(lambda: self.events))
        event = self.events[0]
        self.assertEqual(event["image_status"], "ready")
        self.assertEqual(event["image_metadata"]["md5"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(event["image_quality"], "standard")
        path = Path(event["image_path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.store.directory)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertTrue(path.name.startswith("image-4-"))
        self.assertEqual(event["native_message_id"], "99")
        self.assert_public(event)

    def test_failed_download_is_observable_and_keeps_no_ready_path(self):
        def fail(_metadata, destination):
            Path(destination).write_bytes(b"")
            return False

        receiver = self.make(fail)
        self.assertTrue(receiver.submit(dict(EVENT)))
        self.assertTrue(wait_until(lambda: self.events))
        self.assertEqual(self.events[0]["image_status"], "failed")
        self.assertEqual(self.events[0]["image_path"], "")
        self.assertTrue(self.events[0]["image_error"])
        # 失败下载的临时文件按项目约定保留，不做物理删除。
        self.assertEqual(len(list(self.store.directory.glob(".receive-*.part"))), 1)
        self.assert_public(self.events[0])

    def test_unsupported_image_is_skipped_with_reason(self):
        def skip(_metadata, destination):
            raise ImageDownloadSkipped("仅提供缩略图引用")

        receiver = self.make(skip)
        self.assertTrue(receiver.submit(dict(EVENT)))
        self.assertTrue(wait_until(lambda: self.events))
        event = self.events[0]
        self.assertEqual(event["image_status"], "skipped")
        self.assertEqual(event["image_path"], "")
        self.assertIn("缩略图", event["image_error"])
        self.assertEqual(event["image_metadata"]["md5"], MD5)
        self.assert_public(event)
        # 跳过发生在写文件之前，账号目录里不会留下临时文件。
        self.assertEqual(list(self.store.directory.glob(".receive-*.part")), [])

    def test_invalid_metadata_never_reaches_downloader(self):
        called = []
        receiver = self.make(lambda metadata, destination: called.append(metadata) or False)
        invalid = dict(EVENT, raw_content="<msg />")
        self.assertFalse(receiver.submit(invalid))
        self.assertEqual(called, [])
        self.assertTrue(wait_until(lambda: self.events))
        self.assertEqual(self.events[0]["image_status"], "failed")
        self.assertEqual(self.events[0]["image_error"], "图片元数据无效")
        self.assert_public(self.events[0])

    def test_full_queue_returns_failed_event_without_blocking(self):
        started, release = threading.Event(), threading.Event()

        def blocking(_metadata, destination):
            started.set()
            release.wait(2)
            Path(destination).write_bytes(PNG)
            return True

        receiver = self.make(blocking, queue_size=1)
        try:
            self.assertTrue(receiver.submit(dict(EVENT)))
            self.assertTrue(started.wait(2))
            self.assertTrue(receiver.submit(dict(EVENT, native_message_id="100")))
            self.assertFalse(receiver.submit(dict(EVENT, native_message_id="101")))
            self.assertTrue(wait_until(lambda: any(event["image_status"] == "failed"
                                                   for event in self.events)))
            self.assertIn("101", self.event_ids())
            for event in self.events:
                if event["image_status"] == "failed":
                    self.assert_public(event)
        finally:
            release.set()

    def test_inactive_session_suppresses_delivery(self):
        active = False
        receiver = self.make(self.write_png, is_current=lambda: active)
        self.assertFalse(receiver.submit(dict(EVENT)))
        self.assertEqual(self.events, [])
        active = True
        self.assertTrue(receiver.submit(dict(EVENT)))
        self.assertTrue(wait_until(lambda: self.events))

    def test_delivery_error_does_not_stop_worker(self):
        calls = []

        def fragile(data):
            calls.append(data["native_message_id"])
            if len(calls) == 1:
                raise RuntimeError("delivery failed")

        receiver = self.make(self.write_png, deliver=fragile)
        self.assertTrue(receiver.submit(dict(EVENT, native_message_id="1")))
        self.assertTrue(wait_until(lambda: calls))
        self.assertTrue(receiver.submit(dict(EVENT, native_message_id="2")))
        self.assertTrue(wait_until(lambda: len(calls) == 2))

    def test_close_is_bounded_while_downloader_is_blocked(self):
        started, release = threading.Event(), threading.Event()

        def blocking(_metadata, destination):
            started.set()
            release.wait(3)
            Path(destination).write_bytes(PNG)
            return True

        receiver = self.make(blocking)
        self.assertTrue(receiver.submit(dict(EVENT)))
        self.assertTrue(started.wait(2))
        receiver.close(timeout=0.05)
        self.assertTrue(receiver.is_alive)
        release.set()
        receiver.close(timeout=2)
        self.assertFalse(receiver.is_alive)

    def test_download_target_is_unique_inside_account_directory(self):
        paths = []
        receiver = self.make(self.write_png)
        first = receiver._new_download_path()
        second = receiver._new_download_path()
        paths.extend((first, second))
        self.assertNotEqual(first, second)
        for path in paths:
            self.assertEqual(path.parent, self.store.directory)
            self.assertTrue(path.name.startswith(".receive-"))
            self.assertTrue(path.name.endswith(".part"))
            self.assertFalse(path.exists())

    def test_queue_size_validation(self):
        for value in (0, -1, 1025, True, 1.5):
            with self.assertRaises(ValueError):
                ImageReceiver(self.store, self.write_png, self.record, lambda: True, queue_size=value)


if __name__ == "__main__":
    unittest.main()
