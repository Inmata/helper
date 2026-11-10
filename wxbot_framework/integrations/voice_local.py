"""在独立的 Python 3.12 进程中运行本地 GPT-SoVITS 音色。"""
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
import struct
import subprocess
import sys
from threading import Lock, Thread

_LENGTH = struct.Struct("<I")
_RUNTIME = Path(__file__).resolve().parents[2] / ".runtime/voice-xuanshen"
_SPEAKERS = {"阿梓": "Azusa", "炫神": "XuanShen", "星瞳": "XingTong"}
_PYTHON = _RUNTIME / "venv/Scripts/python.exe"


class LocalVoice:
    def __init__(self, voices):
        self._lock = Lock()
        self._responses = Queue()
        self._process = subprocess.Popen(
            [str(_PYTHON), "-u", "-X", "utf8", __file__, *voices],
            cwd=_RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        Thread(target=self._read_responses, name="LocalVoice", daemon=True).start()

    @staticmethod
    def available_voices():
        if not _PYTHON.is_file():
            return ()
        return tuple(name for name, folder in _SPEAKERS.items() if all(
            (_RUNTIME / "speakers" / folder / file).is_file()
            for file in ("config.json", "ref.wav", "gpt.ckpt", "sovits.pth")))

    def _read_responses(self):
        with self._process.stdout as wire:
            while header := wire.read(_LENGTH.size):
                size, = _LENGTH.unpack(header)
                self._responses.put(wire.read(size))
        self._responses.put(None)

    def synthesize(self, voice, text):
        with self._lock:
            try:
                self._process.stdin.write(f"{voice}\t{text}\n".encode("utf-8"))
                self._process.stdin.flush()
                audio = self._responses.get(timeout=50)
            except Empty:
                self.close()
                raise TimeoutError("本地语音合成超时") from None
            if audio is None:
                raise OSError("本地语音合成没有返回音频")
            return audio or None

    def close(self):
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait()
        self._process.stdin.close()


def _short_samples(model, text):
    import torch

    max_seconds = 2 + len(text) / 4
    # 这些模型每秒解码 25 个 token；parrots 将 t2s_max_sec 乘 50 作为 token 上限。
    model.t2s_max_sec = max_seconds / 2
    for seed in (7, 11):
        torch.manual_seed(seed)
        samples = model.predict(text, text_language="zh", top_k=5)
        if len(samples) <= max_seconds * model.sampling_rate:
            return samples


def _worker():
    import os
    import time

    root = Path.cwd()
    sys.path[:0] = [str(root / "deps-zh"), str(root / "parrots-src")]
    os.environ["NLTK_DATA"] = str(root / "nltk-data")
    os.environ["NUMBA_CACHE_DIR"] = str(root / "numba-cache")
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    started = time.perf_counter()
    import soundfile
    from scipy.signal import resample_poly
    import torch
    from parrots import TextToSpeech, set_log_level

    set_log_level("WARNING")
    models = {}
    for voice in sys.argv[1:]:
        model = TextToSpeech(
            bert_model_path=str(root / "parrots-chinese-roberta-wwm-ext-large"),
            hubert_model_path=str(root / "parrots-chinese-hubert-base"),
            speaker_model_path=str(root / "speakers"), speaker_name=_SPEAKERS[voice],
            device="cuda", half=True, ensure_nltk=False,
        )
        with torch.inference_mode():
            _short_samples(model, "大家好。")
        models[voice] = model
    print(f"本地音色已预热：{'、'.join(models)}，{time.perf_counter() - started:.2f} 秒", file=sys.stderr)
    for line in sys.stdin.buffer:
        voice, text = line.decode("utf-8").split("\t", 1)
        model = models[voice]
        with torch.inference_mode():
            samples = _short_samples(model, text.strip())
        if samples is None:
            wire.write(_LENGTH.pack(0))
            wire.flush()
            continue
        wav = BytesIO()
        samples = resample_poly(samples, 16000, model.sampling_rate)
        soundfile.write(wav, samples, 16000, format="WAV", subtype="PCM_16")
        data = wav.getbuffer()
        wire.write(_LENGTH.pack(len(data)))
        wire.write(data)
        wire.flush()


if __name__ == "__main__":
    _worker()
