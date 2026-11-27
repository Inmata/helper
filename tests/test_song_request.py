import json
from pathlib import Path
import unittest
import wave
from unittest.mock import patch

from plugins.mention_chat import Plugin
from plugins.mention_chat.reply_actions import ReplyActions
from plugins.mention_chat.song_request import Songbook
from support.recyclable_tempdir import RecyclableTemporaryDirectory
from wxbot_framework.conversation.members import MemberStore
from wxbot_framework.integrations.voice_encode import TENCENT_HEADER, prepare_voice
from wxbot_framework.messaging.message import VoiceSubmission
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade


ROOM = "12345678@chatroom"
OTHER = "87654321@chatroom"


class Recorder:
    session_id = "song-test"

    def __init__(self):
        self.voices = []
        self.texts = []
        self.voice_result = VoiceSubmission("song-voice")

    def send_voice(self, room, clip):
        self.voices.append((room, clip))
        return self.voice_result

    def send_text(self, room, text):
        self.texts.append(text)
        return True

    def stop(self):
        return {"dll_unloaded": True}


class SongTests(unittest.TestCase):
    def setUp(self):
        self.temp = RecyclableTemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cfg = self.root / "members.json"
        cfg.write_text(json.dumps({"database_file": "test.db", "history_enabled": True,
                                   "owner_wxid": "wxid_admin", "groups": {ROOM: [], OTHER: []}}), "utf-8")
        self.members = MemberStore(cfg, "wxid_robot")
        self.members.initialize()
        self.addCleanup(self.members.close)
        self.backend = Recorder()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext("mention_chat", {}, self.service, {"voice.send"}, member_store=self.members)
        self.plugin = Plugin()
        self.plugin.members = self.members
        self.plugin.room_state = None
        self.plugin.actions = ReplyActions()
        self.plugin.songs = Songbook(self.root)
        self.sequence = 0
        with wave.open(str(self.root / "确认稿.wav"), "wb") as out:
            out.setparams((2, 2, 44100, 0, "NONE", ""))
            out.writeframes(b"\x01\x10\xff\x0f" * 22050)
        self.write_songs(["雨爱"])

    def write_songs(self, titles):
        rows = [{"title": title, "artist": "杨丞琳" if title == "雨爱" else "孙燕姿", "audio": "确认稿.wav"}
                for title in titles]
        (self.root / "曲库.json").write_text(json.dumps({"songs": rows}, ensure_ascii=False), "utf-8")

    def post(self, text, *, room=ROOM, sender="wxid_member", data=None):
        self.sequence += 1
        msg = Message(text, room, sender_id=sender, is_group=True, session_id=self.backend.session_id,
                      sequence=self.sequence, dispatch_sequence=self.sequence, dispatch_epoch="song",
                      data=data or {})
        self.members.record(msg)
        self.members.turns.observe(msg)
        self.plugin.on_message(msg, self.ctx)

    def set_route(self, scores):
        class Judge:
            def noul(inner, state, questions, **kw):
                if "当前艾特正文" in state:
                    return [0.01, 0.01, *scores]
                return [0.01] * len(questions)
        self.ctx._jev = Judge()

    def test_direct_random_and_named_deliver_native_voice_only(self):
        self.set_route([0.99, 0.99, 0.01])
        self.post("来首《雨爱》")
        self.post("A姐说她想听歌")
        self.post("A姐，唱首歌呗")
        self.set_route([0.99, 0.01, 0.01])
        self.post("@A姐 唱《雨爱》")
        self.assertEqual(len(self.backend.voices), 2)
        for room, clip in self.backend.voices:
            self.assertEqual(room, ROOM)
            self.assertTrue(clip.startswith(TENCENT_HEADER))
            self.assertEqual(prepare_voice(clip)[1], 500)
        self.assertEqual(self.backend.texts, [])
        receipts = self.members.connection().execute("SELECT kind,status FROM reply_receipts").fetchall()
        self.assertEqual([tuple(r) for r in receipts], [("voice", "submitted")] * 2)

    def test_missing_duplicate_admin_and_fulfilled(self):
        self.set_route([0.99, 0.01, 0.01])
        self.post("A姐，唱《遇见》")
        self.post("A姐，唱孙燕姿的《遇见》", room=OTHER)
        self.assertIn("已加入", self.backend.texts[0])
        self.assertIn("已经在", self.backend.texts[1])
        self.post("/想听清单")
        self.assertNotIn("遇见", self.backend.texts[-1])
        self.post("/想听清单", sender="wxid_admin")
        self.assertEqual(self.backend.texts[-1].count("《遇见》"), 1)
        self.write_songs(["雨爱", "遇见"])
        self.post("/想听清单", sender="wxid_admin")
        self.assertIn("没有待补录", self.backend.texts[-1])
        self.post("A姐，唱孙燕姿的《遇见》")
        self.assertEqual(len(self.backend.voices), 1)

    def test_distinct_performers_and_versions_stay_pending(self):
        for artist, version in [("甲", "录音室版"), ("乙", ""), ("甲", "现场版")]:
            Songbook.register(self.ctx, "遇见", artist, version)
        self.assertIn("已经在", Songbook.register(self.ctx, "遇见", "甲", "现场版"))
        self.post("/想听清单", sender="wxid_admin")
        self.assertEqual(self.backend.texts, ["待补录的歌\n甲的《遇见》（录音室版）\n乙的《遇见》\n甲的《遇见》（现场版）"])
        (self.root / "曲库.json").write_text(json.dumps({"songs": [
            {"title": "遇见（现场版）", "artist": "甲", "audio": "确认稿.wav"}]}), "utf-8")
        self.post("/想听清单", sender="wxid_admin")
        self.assertEqual(self.backend.texts[-1], "待补录的歌\n甲的《遇见》（录音室版）\n乙的《遇见》")

    def test_direct_nickname_with_another_members_quote(self):
        self.set_route([0.99, 0.01, 0.01])
        class Model:
            def complete_json(inner, system, user, **options):
                task = json.loads(user)
                self.assertEqual(task["引用"], "我最近喜欢《雨爱》")
                return {"歌名": "雨爱", "歌手": "", "版本": ""}
        self.ctx._llm = Model()
        self.post("A姐，唱这首", data={"quote": {"fromusr": "wxid_other", "text": "我最近喜欢《雨爱》"}})
        self.assertEqual(len(self.backend.voices), 1)

    def test_catalog_and_clip_failure_are_not_wishes(self):
        self.set_route([0.99, 0.01, 0.01])
        (self.root / "曲库.json").write_text("{}", "utf-8")
        self.post("A姐，唱《遇见》")
        self.assertIn("读不到曲库", self.backend.texts[-1])
        self.write_songs(["雨爱"])
        (self.root / "确认稿.wav").write_bytes(b"damaged")
        self.post("A姐，唱《雨爱》")
        self.assertIn("播不了", self.backend.texts[-1])
        self.assertEqual(self.members.connection().execute("SELECT count(*) FROM song_wishes").fetchone()[0], 0)

    def test_failed_registration_and_unconfirmed_voice(self):
        self.set_route([0.99, 0.01, 0.01])
        self.members.connection().execute("CREATE TRIGGER fail_wish BEFORE INSERT ON song_wishes BEGIN "
                                          "SELECT RAISE(FAIL,'write failed'); END")
        with self.assertLogs(self.ctx.logger, "ERROR"):
            self.post("A姐，唱《遇见》")
        self.assertIn("没记上", self.backend.texts[-1])
        self.backend.voice_result = False
        self.post("A姐，唱《雨爱》")
        self.assertEqual(len(self.backend.voices), 1)
        self.assertEqual(len(self.backend.texts), 1)
        self.assertEqual(self.members.connection().execute(
            "SELECT status FROM reply_receipts WHERE kind='voice'").fetchone()[0], "unconfirmed")

    def test_song_list_and_music_discussion(self):
        self.set_route([0.01, 0.01, 0.99])
        self.post("A姐，你能唱哪些歌？")
        self.assertIn("《雨爱》", self.backend.texts[-1])
        self.set_route([0.01, 0.01, 0.01])
        with patch.object(self.plugin, "_reply_turn", return_value=iter(())) as chat:
            self.post("A姐，雨爱是谁唱的？")
            chat.assert_called_once()
        self.assertFalse(self.backend.voices)
