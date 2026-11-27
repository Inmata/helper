"""语音进程预热、合成超时及后续恢复；用真实子进程验证内存协议。"""
from contextlib import nullcontext
import logging
from pathlib import Path
from queue import Queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.mention_chat import Plugin
from wxbot_framework.conversation.slow_replies import SlowReplies
from plugins.mention_chat.voice_reply import VoiceReply, voice_parts
from wxbot_framework.integrations import voice_local
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget
from wxbot_framework.messaging.message import Message, VoiceSubmission


WORKER = """
import struct, sys, time
time.sleep(float(sys.argv[1]))
if sys.argv[2] == 'exit':
    sys.exit(1)
wire = sys.stdout.buffer
wire.write(b'READY\\n')
wire.flush()
for line in sys.stdin.buffer:
    if sys.argv[2] == 'hang':
        time.sleep(10)
    audio = b'' if sys.argv[2] == 'long' else b'audio'
    if sys.argv[2] == 'echo':
        if b'first' in line:
            time.sleep(0.3)
        if b'slow' in line:
            time.sleep(5.5)
        audio = line.rstrip().split(b'\\t', 1)[1]
    wire.write(struct.pack('<I', len(audio)) + audio)
    wire.flush()
"""


class FastQueue(Queue):
    def get(self, block=True, timeout=None):
        return super().get(block, min(timeout, 0.15) if timeout is not None else None)


class VoiceTests(unittest.TestCase):
    def test_automatic_voice_35_percent_thirty_second_cooldown_and_explicit_preference(self):
        reply = VoiceReply()
        message = Message('晚安', '123@chatroom')
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1000), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0.35):
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1000), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0.34):
            self.assertTrue(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1029), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0):
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1030), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0):
            self.assertFalse(reply.reserve(message, '晚安。', False, automatic=True))
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=False))
            self.assertFalse(reply.reserve(message, '字' * 81, None, automatic=True))
            self.assertTrue(reply.reserve(message, '字' * 80, None, automatic=True))
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=True))
            self.assertTrue(reply.reserve(message, '指定原话。', True, automatic=False))

    def test_voice_parts_preserve_long_unpunctuated_text(self):
        text = '甲' * 79 + '😀' + 'abc' * 30
        parts = voice_parts(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part) <= 80 for part in parts))

    def test_xingtong_selects_spoken_language_and_keeps_mixed_text_in_order(self):
        for text, expected in [
            ("晚上好。", [("zh", "晚上好。")]),
            ("東京", [("zh", "東京")]),
            ("今天是2026年。", [("zh", "今天是2026年。")]),
            ("3.14", [("zh", "3.14")]),
            ("Good morning! How's it going?", [("en", "Good morning! How's it going?")]),
            ("今日はいい天気ですね。", [("ja", "今日はいい天気ですね。")]),
            ("大家好，Good morning! 今日は晴れ。", [
                ("zh", "大家好，"), ("en", "Good morning!"), ("ja", " 今日は晴れ。")]),
            ("试试Hello World这个词", [("zh", "试试"), ("en", "Hello World"), ("zh", "这个词")]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(voice_local._xingtong_passages(text), expected)

    def test_xingtong_joins_complete_passages_and_other_voices_use_chinese(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('音频数组检查需要 NumPy')

        chinese = np.full(300, 0.1, dtype=np.float32)
        english = np.full(400, 0.2, dtype=np.float32)
        model = SimpleNamespace(sampling_rate=1000, predict=Mock(side_effect=[chinese, english, chinese]))
        with patch.dict(sys.modules, {'torch': SimpleNamespace(manual_seed=Mock())}):
            samples = voice_local._voice_samples(model, '星瞳', '你好，Hello！')
            np.testing.assert_array_equal(samples, np.concatenate([chinese, english]))
            voice_local._voice_samples(model, '炫神', '你好，Hello！')
            self.assertEqual([(c.args[0], c.kwargs['text_language']) for c in model.predict.call_args_list],
                             [('你好，', 'zh'), ('Hello!', 'en'), ('你好，Hello！', 'zh')])
            model.predict.side_effect = [chinese, np.ones(15000), chinese, np.ones(15000)]
            self.assertIsNone(voice_local._voice_samples(model, '星瞳', '你好，Hello！'))

    def worker(self, *, delay=0, mode='audio'):
        launch = subprocess.Popen

        def spawn(*args, **options):
            options['cwd'] = Path(__file__).resolve().parents[1]
            return launch([sys.executable, '-u', '-c', WORKER, str(delay), mode], **options)

        with patch.object(voice_local.subprocess, 'Popen', side_effect=spawn):
            local = voice_local.LocalVoice('星瞳')
        self.addCleanup(local.close)
        return local

    def context(self):
        return SimpleNamespace(logger=logging.getLogger('test.voice'),
                               reply_voice=Mock(return_value=VoiceSubmission('test-voice')))

    def test_cold_start_does_not_consume_synthesis_timeout(self):
        with patch.object(voice_local, 'Queue', FastQueue):
            local = self.worker(delay=0.3)
        self.assertEqual(local.synthesize('星瞳', '测试。'), b'audio')

    def test_warmup_timeout_closes_worker(self):
        local = self.worker(delay=10)
        with patch.object(voice_local, '_WARMUP_TIMEOUT', 0.05, create=True):
            with self.assertRaisesRegex(TimeoutError, '预热'):
                local.synthesize('星瞳', '测试。')
        self.assertIsNotNone(local._process.poll())

    def test_worker_exits_during_warmup(self):
        local = self.worker(mode='exit')
        with self.assertRaises(OSError):
            local.synthesize('星瞳', '测试。')

    def test_synthesis_timeout_closes_worker(self):
        with patch.object(voice_local, 'Queue', FastQueue):
            local = self.worker(mode='hang')
        with self.assertRaisesRegex(TimeoutError, '合成'):
            local.synthesize('星瞳', '测试。')
        self.assertIsNotNone(local._process.poll())

    def test_overlong_audio_keeps_worker_for_next_request(self):
        local = self.worker(mode='long')
        self.assertIsNone(local.synthesize('星瞳', '测试。'))
        self.assertIsNone(local._process.poll())

    def test_long_leading_silence_is_trimmed_before_duration_check(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('音频数组检查需要 NumPy')
        recording = np.concatenate((np.zeros(9700), np.full(2700, 0.2)))
        model = SimpleNamespace(sampling_rate=1000, predict=Mock(return_value=recording))
        with patch.dict(sys.modules, {'torch': SimpleNamespace(manual_seed=Mock())}):
            samples = voice_local._voice_samples(model, '星瞳', '蔡子辉这个扎啤 真是笑死我了')
        self.assertIsNotNone(samples)
        self.assertLess(len(samples), 3000)
        self.assertAlmostEqual(float(samples[-1]), 0.2)
        model.predict.assert_called_once()

    def test_next_request_recreates_worker_after_failure(self):
        for error in (TimeoutError('合成超时'), BrokenPipeError('进程退出')):
            with self.subTest(error=type(error).__name__):
                reply = VoiceReply()
                reply._voices = ('星瞳',)
                failed = Mock()
                failed.synthesize.side_effect = error
                reply._local = failed
                ctx = self.context()
                message = Message('用语音念。', '123@chatroom')
                self.assertIsNone(reply.send(message, ctx, '测试。', voice='星瞳', guard=nullcontext))
                recovered = Mock()
                recovered.synthesize.return_value = b'audio'
                with patch('plugins.mention_chat.voice_reply.LocalVoice', return_value=recovered) as create:
                    self.assertEqual(reply.send(message, ctx, '再试。', voice='星瞳', guard=nullcontext),
                                     VoiceSubmission('test-voice'))
                create.assert_called_once_with('星瞳')
                recovered.synthesize.assert_called_once_with('星瞳', '再试。')
                failed.close.assert_called_once()
                reply.close()

    def test_automatic_voice_can_finish_after_five_seconds_with_time_left_in_turn(self):
        reply = VoiceReply()
        reply._voices = ('星瞳',)
        local = reply._local = self.worker(mode='echo')
        local.wait_ready()
        ctx = self.context()
        token = CURRENT.set(ReplyBudget(time.monotonic(), seconds=20))
        try:
            sent = reply.send(Message('', '123@chatroom'), ctx, 'slow', voice='星瞳',
                              guard=nullcontext, automatic=True)
        finally:
            CURRENT.reset(token)
        self.assertTrue(sent)
        self.assertEqual(ctx.reply_voice.call_args.args[1], b'slow')

    def test_automatic_timeout_keeps_worker_and_discards_only_the_late_audio(self):
        reply = VoiceReply()
        reply._voices = ('星瞳',)
        local = reply._local = self.worker(mode='echo')
        local.wait_ready()
        ctx = self.context()
        message = Message('', '123@chatroom')
        token = CURRENT.set(ReplyBudget(time.monotonic(), seconds=2.1))
        try:
            self.assertIsNone(reply.send(message, ctx, 'first', voice='星瞳', guard=nullcontext, automatic=True))
        finally:
            CURRENT.reset(token)
        ctx.reply_voice.assert_not_called()
        self.assertIsNone(local._process.poll())
        self.assertTrue(reply.send(message, ctx, 'second', voice='星瞳', guard=nullcontext, automatic=True))
        self.assertEqual(ctx.reply_voice.call_args.args[1], b'second')

    def test_automatic_request_starts_replacement_and_keeps_warming_after_short_turn(self):
        reply = VoiceReply()
        reply._voices = ('星瞳',)
        ctx = self.context()
        message = Message('', '123@chatroom')
        replacement = self.worker(delay=0.5)
        with patch('plugins.mention_chat.voice_reply.LocalVoice', return_value=replacement) as create:
            self.assertIsNone(reply.send(message, ctx, '启动', voice='星瞳', guard=nullcontext, automatic=True))
            create.assert_called_once_with('星瞳')
            token = CURRENT.set(ReplyBudget(time.monotonic(), seconds=0.05))
            try:
                self.assertIsNone(reply.send(message, ctx, '明示语音', voice='星瞳', guard=nullcontext))
            finally:
                CURRENT.reset(token)
            ctx.reply_voice.assert_not_called()
            replacement.wait_ready()
            self.assertTrue(reply.send(message, ctx, '已就绪', voice='星瞳', guard=nullcontext, automatic=True))
            create.assert_called_once()

    def test_shutdown_cancels_warmup_without_restarting_worker(self):
        plugin = Plugin()
        plugin.voice_reply._voices = ('星瞳',)
        local = self.worker(delay=10)
        plugin.voice_reply._local = local
        ctx = self.context()
        plugin._slow = SlowReplies(ctx.logger)
        entered = threading.Event()

        def turn():
            entered.set()
            plugin.voice_reply.send(Message('', '123@chatroom'), ctx, '测试。',
                                    voice='星瞳', guard=nullcontext)
            yield

        plugin._slow.handoff('test', turn())
        self.assertTrue(entered.wait(1))
        with patch('plugins.mention_chat.voice_reply.LocalVoice') as create:
            started = time.monotonic()
            plugin.on_unload(ctx)
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(plugin.voice_reply.send(Message('', '123@chatroom'), ctx, '再试。',
                                                     voice='星瞳', guard=nullcontext))
            create.assert_not_called()
        ctx.reply_voice.assert_not_called()
