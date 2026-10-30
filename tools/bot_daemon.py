"""后台守护：保持 main.py 的标准输入管道，并转发控制文件里的命令。

由 ``tools/bot_ctl.ps1`` 通过 Windows 计划任务启动，不直接面向用户。
控制文件固定为 ``.runtime/bot_stdin.txt``：每行一条控制台命令，写入
``quit`` 即按正常退出流程停止机器人并卸载桥接。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
CONTROL = RUNTIME / "bot_stdin.txt"
OUTPUT = RUNTIME / "bot_stdout.log"
DAEMON_LOG = RUNTIME / "bot_daemon.log"
POLL_SECONDS = 0.2


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with DAEMON_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def read_new(path, offset):
    """读取控制文件自 offset 之后的新内容；文件被重建时从头读。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        path.write_text("", encoding="utf-8")
        return 0, b""
    if size < offset:
        offset = 0
    if size == offset:
        return offset, b""
    with path.open("rb") as handle:
        handle.seek(offset)
        return size, handle.read(size - offset)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="运行 main.py 的解释器")
    parser.add_argument("--entry", default=str(ROOT / "main.py"), help="机器人入口脚本")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CONTROL.write_text("", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log(f"守护启动：entry={args.entry}")
    with OUTPUT.open("w", encoding="utf-8", errors="replace") as output:
        child = subprocess.Popen(
            [args.python, "-X", "utf8", "-B", args.entry],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
            creationflags=creationflags, env=environment)
        log(f"main.py 已启动：pid={child.pid}")
        offset, buffered = 0, b""
        try:
            while child.poll() is None:
                offset, data = read_new(CONTROL, offset)
                if data:
                    buffered += data
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        command = line.decode("utf-8", errors="replace").strip()
                        if command:
                            log(f"转发命令：{command}")
                            child.stdin.write((command + "\n").encode("utf-8"))
                            child.stdin.flush()
                time.sleep(POLL_SECONDS)
        except (BrokenPipeError, OSError):
            log("控制管道已断开")
        except Exception:
            log("守护循环异常：\n" + traceback.format_exc())
            child.terminate()
        finally:
            try:
                child.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            code = child.wait()
            log(f"main.py 已退出：code={code}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("守护异常退出：\n" + traceback.format_exc())
        raise
