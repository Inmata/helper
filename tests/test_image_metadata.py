"""图片 XML 解析与本地保存的离线回归；不访问网络或微信进程。"""
from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.recyclable_tempdir import RecyclableTemporaryDirectory
from wxbot_framework.image_metadata import (ImageMetadataError, ImageStore, parse_image_metadata)


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
JPEG = b"\xff\xd8\xff" + b"test-image"
XML = ('<msg><img aeskey="0123456789abcdef0123456789abcdef" cdnbigimgurl="https://cdn/big" '
       'cdnmidimgurl="https://cdn/mid" cdnthumburl="https://cdn/thumb" '
       'cdnthumbaeskey="0123456789abcdef" cdnthumblength="120" cdnthumbheight="40" '
       'cdnthumbwidth="30" cdnmidheight="640" cdnmidwidth="480" encryver="1" filekey="mid-key" '
       'hdlength="2048" height="1280" length="1024" md5="fedcba9876543210fedcba9876543210" '
       'width="960" /></msg>')


class ParseImageMetadataTests(unittest.TestCase):
    def test_reads_public_fields_and_normalizes_md5(self):
        metadata = parse_image_metadata(XML)
        self.assertEqual(metadata.md5, "fedcba9876543210fedcba9876543210")
        self.assertEqual((metadata.width, metadata.height, metadata.length), (960, 1280, 1024))
        self.assertEqual((metadata.cdnmidwidth, metadata.cdnmidheight), (480, 640))
        self.assertEqual(metadata.encryver, 1)
        self.assertEqual(metadata.filekey, "mid-key")

    def test_secrets_are_not_part_of_repr(self):
        metadata = parse_image_metadata(XML)
        rendered = repr(metadata)
        for secret in ("cdn/big", "cdn/mid", "cdn/thumb", "0123456789abcdef0123456789abcdef",
                       "mid-key", "0123456789abcdef\""):
            self.assertNotIn(secret, rendered)

    def test_rejects_invalid_xml_without_echoing_content(self):
        cases = (
            "",
            "not xml",
            "<img />",
            "<msg></msg>",
            "<msg><img /><img /></msg>",
            "<msg><not-image><img /></not-image></msg>",
            '<!DOCTYPE msg [<!ENTITY x "y">]><msg><img md5="0123456789abcdef0123456789abcdef" /></msg>',
            '<msg><img md5="not-a-md5" /></msg>',
            '<msg><img width="-1" /></msg>',
            '<msg><img width="999999" /></msg>',
            '<msg><img filekey="' + "k" * 1025 + '" /></msg>',
            '<msg><img filekey="bad\x01key" /></msg>',
            "<msg><img /></msg>" + " " * 17000,
        )
        for content in cases:
            with self.subTest(content=content[:40]):
                with self.assertRaises(ImageMetadataError) as caught:
                    parse_image_metadata(content)
                if content:
                    self.assertNotIn(content[:12], str(caught.exception))

    def test_non_string_input_is_rejected(self):
        for value in (None, b"<msg />", 3):
            with self.assertRaises(ImageMetadataError):
                parse_image_metadata(value)


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = RecyclableTemporaryDirectory(prefix="wxbot-image-store-")
        self.root = Path(self.temporary.name) / "images"
        self.store = ImageStore(self.root, "wxid_image_store")

    def tearDown(self):
        self.temporary.cleanup()

    def test_account_directory_is_hashed_and_isolated(self):
        other = ImageStore(self.root, "wxid_other_account")
        self.assertNotEqual(self.store.directory, other.directory)
        self.assertEqual(self.store.directory.parent, self.store.root)
        self.assertTrue(self.store.directory.name.startswith("account-"))
        self.assertEqual(len(self.store.directory.name), len("account-") + 64)
        self.assertNotIn("wxid_image_store", str(self.store.directory))

    def test_save_writes_once_and_keeps_original_bytes(self):
        first = self.store.save(PNG, sequence=7)
        second = self.store.save(PNG, sequence=7)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.store.directory)
        self.assertEqual(first.suffix, ".png")
        self.assertIn("image-7-", first.name)
        self.assertEqual(first.read_bytes(), PNG)
        self.assertFalse(list(first.parent.glob(".image-*")))

    def test_save_detects_format_and_rejects_unknown_data(self):
        self.assertEqual(self.store.save(JPEG).suffix, ".jpg")
        for data in (b"", b"<msg><img /></msg>", b"x" * (20 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                self.store.save(data)

    def test_commit_file_moves_valid_part_without_copying(self):
        self.store.directory.mkdir(parents=True)
        part = self.store.directory / ".receive-test.part"
        part.write_bytes(PNG)
        committed = self.store.commit_file(part, sequence=12)
        self.assertFalse(part.exists())
        self.assertTrue(committed.exists())
        self.assertEqual(committed.read_bytes(), PNG)
        self.assertIn("image-12-", committed.name)

    def test_commit_file_rejects_outside_or_invalid_source(self):
        outside = Path(self.temporary.name) / "outside.part"
        outside.write_bytes(PNG)
        with self.assertRaises(ValueError):
            self.store.commit_file(outside)
        self.assertTrue(outside.exists())
        empty = self.store.directory / ".receive-empty.part"
        self.store.directory.mkdir(parents=True)
        empty.write_bytes(b"")
        with self.assertRaises(ValueError):
            self.store.commit_file(empty)
        self.assertTrue(empty.exists())

    def test_invalid_account_is_rejected(self):
        for account in ("", "filehelper", "1@chatroom", "gh_news"):
            with self.assertRaises(ValueError):
                ImageStore(self.root, account)


if __name__ == "__main__":
    unittest.main()
