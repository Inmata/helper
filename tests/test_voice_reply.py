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
from plugins.mention_chat.slow_replies import SlowReplies
from plugins.mention_chat.voice_reply import VoiceReply, voice_parts
from wxbot_framework.integrations import voice_local
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
    wire.write(struct.pack('<I', len(audio)) + audio)
    wire.flush()
"""


class FastQueue(Queue):
    def get(self, block=True, timeout=None):
        return super().get(block, min(timeout, 0.15) if timeout is not None else None)


class VoiceTests(unittest.TestCase):
    def test_automatic_voice_half_chance_ten_second_cooldown_and_explicit_preference(self):
        reply = VoiceReply()
        message = Message('晚安', '123@chatroom')
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1000), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0.5):
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1000), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0.49):
            self.assertTrue(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1009), \
                patch('plugins.mention_chat.voice_reply.random', return_value=0):
            self.assertFalse(reply.reserve(message, '晚安。', None, automatic=True))
        with patch('plugins.mention_chat.voice_reply.monotonic', return_value=1010), \
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
            samples = voice_local._short_samples(model, '蔡子辉这个扎啤 真是笑死我了')
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
