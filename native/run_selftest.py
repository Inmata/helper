"""版本锁定的原生桥接，只开放当前用户的文件传输助手。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import json
import argparse
import hashlib
from pathlib import Path
import sys
import time
import uuid

import pefile

ROOT = Path(__file__).resolve().parent.parent
DLL = ROOT / "native/build/selftest_bridge.dll"
TEXT_DLL = ROOT / "native/build_text/text_bridge.dll"
K = c.WinDLL("kernel32", use_last_error=True)
PTR = c.c_void_p

K.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; K.OpenProcess.restype = w.HANDLE
K.CloseHandle.argtypes = [w.HANDLE]; K.CloseHandle.restype = w.BOOL
K.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
K.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]; K.CreateToolhelp32Snapshot.restype = w.HANDLE
K.GetModuleHandleW.argtypes = [w.LPCWSTR]; K.GetModuleHandleW.restype = w.HMODULE
K.GetProcAddress.argtypes = [w.HMODULE, c.c_char_p]; K.GetProcAddress.restype = PTR
K.GetModuleHandleExW.argtypes = [w.DWORD, PTR, c.POINTER(w.HMODULE)]
K.GetModuleFileNameW.argtypes = [w.HMODULE, w.LPWSTR, w.DWORD]
K.VirtualAllocEx.argtypes = [w.HANDLE, PTR, c.c_size_t, w.DWORD, w.DWORD]; K.VirtualAllocEx.restype = PTR
K.VirtualFreeEx.argtypes = [w.HANDLE, PTR, c.c_size_t, w.DWORD]
K.WriteProcessMemory.argtypes = [w.HANDLE, PTR, PTR, c.c_size_t, c.POINTER(c.c_size_t)]
K.ReadProcessMemory.argtypes = [w.HANDLE, PTR, PTR, c.c_size_t, c.POINTER(c.c_size_t)]
K.CreateRemoteThread.argtypes = [w.HANDLE, PTR, c.c_size_t, PTR, PTR, w.DWORD, c.POINTER(w.DWORD)]
K.CreateRemoteThread.restype = w.HANDLE
K.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]; K.WaitForSingleObject.restype = w.DWORD
K.GetExitCodeThread.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]


class Module(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD), ("modBaseAddr", PTR),
                ("modBaseSize", w.DWORD), ("hModule", w.HMODULE),
                ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260)]


class Config(c.Structure):
    _fields_ = [("size", w.DWORD), ("pid", w.DWORD), ("manager", c.c_uint64),
                ("log_path", w.WCHAR * 260), ("text", c.c_char * 1024)]


class TextConfig(Config):
    _fields_ = [("target", c.c_char * 128)]

class AtTextConfig(TextConfig):
    _fields_ = [("members", c.c_char * 1024)]

class ImageDownloadConfig(Config):
    _fields_ = [("filekey", c.c_char * 1024), ("aeskey", c.c_char * 65),
                ("image_type", c.c_uint32)]


assert c.sizeof(Config) == 1560 and c.sizeof(TextConfig) == 1688 and c.sizeof(AtTextConfig) == 2712
assert c.sizeof(ImageDownloadConfig) == 2656


class ProcessEntry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD),
                ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("exe", w.WCHAR * 260)]


K.Module32FirstW.argtypes = [w.HANDLE, c.POINTER(Module)]
K.Module32NextW.argtypes = [w.HANDLE, c.POINTER(Module)]
K.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
K.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]


def check(result, label):
    if not result:
        raise c.WinError(c.get_last_error(), f"{label} 失败")
    return result


def modules(pid):
    snapshot = K.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    result = {}
    try:
        entry = Module(); entry.dwSize = c.sizeof(entry)
        check(K.Module32FirstW(snapshot, c.byref(entry)), "读取模块")
        while True:
            result[entry.szModule.lower()] = (int(entry.modBaseAddr), entry.szExePath)
            if not K.Module32NextW(snapshot, c.byref(entry)): break
    finally:
        K.CloseHandle(snapshot)
    return result


def remote_system_function(pid, name):
    address = check(K.GetProcAddress(K.GetModuleHandleW("kernel32.dll"), name), "查找系统函数")
    owner = w.HMODULE()
    check(K.GetModuleHandleExW(0x4 | 0x2, address, c.byref(owner)), "读取函数所属模块")
    path = c.create_unicode_buffer(32768)
    check(K.GetModuleFileNameW(owner, path, len(path)), "读取系统模块名")
    remote_base = modules(pid)[Path(path.value).name.lower()][0]
    return remote_base + address - owner.value


def remote_call(process, address, parameter, timeout=30000):
    thread = check(K.CreateRemoteThread(process, None, 0, address, parameter, 0, None), "创建自测线程")
    try:
        status = K.WaitForSingleObject(thread, timeout)
        if status != 0:
            raise TimeoutError(f"自测线程未完成，等待结果 {status}；不要终止该线程")
        result = w.DWORD()
        check(K.GetExitCodeThread(thread, c.byref(result)), "读取自测结果")
        return result.value
    finally:
        K.CloseHandle(thread)


def wechat_candidates():
    snapshot = K.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
    candidates = []
    try:
        entry = ProcessEntry(); entry.dwSize = c.sizeof(entry)
        check(K.Process32FirstW(snapshot, c.byref(entry)), "枚举微信进程")
        while True:
            if entry.exe.lower() == "weixin.exe":
                try:
                    if "weixin.dll" in modules(entry.pid): candidates.append(entry.pid)
                except OSError:
                    pass
            if not K.Process32NextW(snapshot, c.byref(entry)): break
    finally:
        K.CloseHandle(snapshot)
    return candidates


def find_wechat():
    candidates = wechat_candidates()
    if len(candidates) != 1:
        raise RuntimeError(f"需要唯一的微信主进程，当前候选为 {candidates}；可指定 --pid")
    return candidates[0]


def download_aeskey_supported(aeskey):
    """原生图片下载接口的密钥规则：32 或 64 位十六进制。

    与 image_download.h 的校验保持一致；缩略图 XML 的 cdnthumbaeskey 是 16 位，
    不能用于该接口，调用方应按 skipped 处理而不是猜测性转换。
    """
    return (isinstance(aeskey, str) and len(aeskey) in (32, 64) and
            all(ch in "0123456789abcdefABCDEF" for ch in aeskey))


def _validate_download_image(download_image):
    """校验图片下载参数；返回 (destination, filekey, aeskey, image_type)。"""
    if not isinstance(download_image, dict):
        raise ValueError("download_image 必须是对象")
    destination = download_image.get("destination")
    filekey = download_image.get("filekey")
    aeskey = download_image.get("aeskey")
    image_type = download_image.get("image_type")
    if not isinstance(destination, str) or not destination.strip() or "\0" in destination:
        raise ValueError("图片下载目标必须是非空本地路径")
    path = Path(destination)
    if not path.is_absolute():
        raise ValueError("图片下载目标必须是绝对路径")
    encoded = str(path).encode("utf-8")
    if len(encoded) >= 1024:
        raise ValueError("图片下载目标路径过长")
    if not path.parent.is_dir():
        raise ValueError("图片下载目标的父目录必须存在")
    if path.exists() or path.is_symlink():
        raise ValueError("图片下载目标必须尚不存在")
    if not isinstance(filekey, str) or not filekey or len(filekey.encode("ascii", "ignore")) != len(filekey) \
            or len(filekey) >= 1024 or "\0" in filekey:
        raise ValueError("图片 filekey 必须是 1–1023 个 ASCII 字节")
    if not download_aeskey_supported(aeskey):
        raise ValueError("图片 aeskey 必须是 32 或 64 位十六进制字符串")
    if type(image_type) is not int or image_type not in (1, 2, 3):
        raise ValueError("图片下载类型必须是 1、2 或 3")
    return str(path), filekey, aeskey, image_type


def run(mode, *, pid=None, text="WXNATIVE_TEST_3", keep_loaded=False, preserve_report=True,
        bridge_path=None, target="filehelper", mentions=None, download_image=None):
    dll = Path(bridge_path or DLL).resolve()
    if dll not in (DLL.resolve(), TEXT_DLL.resolve()):
        raise ValueError("不支持的桥接 DLL 路径")
    generic = dll == TEXT_DLL.resolve()
    if generic and mode not in ("--dispatch", "--listen", "--remove", "--probe-at", "--download-image"):
        raise ValueError("通用桥接只支持文本/图片、监听和卸载")
    if not generic and mode == "--download-image":
        raise ValueError("图片下载仅用于通用桥接")
    if not generic and target != "filehelper":
        raise ValueError("旧自测桥接仅支持文件传输助手")
    if generic:
        from wxbot_framework.messaging.text import validate_target
        validate_target(target)
    if mentions is not None:
        if not generic or mode != "--dispatch":
            raise ValueError("艾特成员列表仅用于通用文本发送")
        from wxbot_framework.messaging.text import validate_mentions
        mentions = validate_mentions(target, mentions)
    if mode not in ("--arm", "--dispatch", "--receive", "--listen", "--remove", "--probe-at", "--download-image"):
        raise RuntimeError("不支持的原生桥接操作")
    if mode == "--probe-at" and not generic:
        raise ValueError("艾特探针仅用于通用桥接")
    if download_image is not None and mode != "--download-image":
        raise ValueError("图片下载参数仅用于 --download-image")
    if mode == "--download-image" and download_image is None:
        raise ValueError("--download-image 需要下载参数")
    if keep_loaded and mode not in ("--dispatch", "--download-image"):
        raise ValueError("keep_loaded 只用于接收服务内的发送或图片下载")
    if c.sizeof(PTR) != 8: raise RuntimeError("需要 64 位 Python")
    destination = None
    if download_image is not None:
        destination, filekey, aeskey, image_type = _validate_download_image(download_image)
    encoded = text.encode("utf-8")
    if mode != "--download-image" and (not encoded or len(encoded) > 1023 or b"\0" in encoded):
        raise ValueError("文本应为 1–1023 个 UTF-8 字节，且不能包含 NUL")
    pid = pid or find_wechat()
    process = check(K.OpenProcess(0x43a, False, pid), "打开已验证的微信进程")
    allocations = []
    pending_thread = False
    try:
        path = c.create_unicode_buffer(32768); length = w.DWORD(len(path))
        check(K.QueryFullProcessImageNameW(process, 0, path, c.byref(length)), "核对客户端路径")
        client = Path(path.value)
        if client.name.lower() != "weixin.exe": raise RuntimeError("目标不是微信")
        version = pefile.PE(str(client)).VS_FIXEDFILEINFO[0]
        if (version.FileVersionMS, version.FileVersionLS) != (0x40001, 0xf000d):
            raise RuntimeError("客户端版本已改变")
        loaded_modules = modules(pid)
        core = loaded_modules.get("weixin.dll")
        if not core: raise RuntimeError("微信核心模块尚未加载")
        baseline = json.loads((ROOT / "native/compatibility.json").read_text(encoding="utf-8"))
        if hashlib.sha256(Path(core[1]).read_bytes()).hexdigest() != baseline["sha256"]:
            raise RuntimeError("微信核心 DLL 指纹不匹配，拒绝加载")
        other = DLL if generic else TEXT_DLL
        if mode != "--remove" and other.name.lower() in loaded_modules:
            raise RuntimeError("另一版本的桥接仍在运行，请先在旧 main.py 中输入 quit")
        pe = pefile.PE(str(dll))
        # 两种桥接的导出表不同，必须先按桥接类型选映射；图片下载只存在于通用桥接。
        if generic:
            name = {"--dispatch": b"WxSendText", "--listen": b"WxStartTextReceiver",
                    "--remove": b"WxRemoveSelfTest", "--probe-at": b"WxArmMentionProbe",
                    "--download-image": b"WxDownloadImage"}[mode]
            if mentions is not None:
                name = b"WxSendAtText"
        else:
            name = b"WxArmMentionProbe" if mode == "--probe-at" else {
                "--arm":b"WxArmSelfTest", "--dispatch":b"WxDispatchSelfTest", "--receive":b"WxArmReceiveTest",
                "--listen":b"WxStartFileHelperReceiver", "--remove":b"WxRemoveSelfTest"}[mode]
        export = next(item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name == name)

        def allocate(data):
            pointer = check(K.VirtualAllocEx(process, None, len(data), 0x3000, 0x04), "分配自测参数")
            allocations.append(pointer)
            buffer = c.create_string_buffer(data); written = c.c_size_t()
            check(K.WriteProcessMemory(process, pointer, buffer, len(data), c.byref(written)), "写入自测参数")
            if written.value != len(data): raise RuntimeError("参数写入不完整")
            return pointer

        if mode in ("--arm", "--dispatch", "--receive", "--listen", "--probe-at"):
            existing = dll.name.lower() in loaded_modules
            if existing and not keep_loaded:
                raise RuntimeError("微信进程中已有桥接 DLL；请先在旧机器人输入 quit 并等待框架停止。"
                                   "若旧实例已退出，运行 python tools/recover_bridge.py 清理后再启动")
            if not existing:
                if keep_loaded: raise RuntimeError("接收服务的桥接 DLL 已卸载")
                dll_parameter = allocate((str(dll) + "\0").encode("utf-16-le"))
                remote_call(process, remote_system_function(pid, b"LoadLibraryW"), dll_parameter)
        loaded = modules(pid).get(dll.name.lower())
        if not loaded or Path(loaded[1]).resolve() != dll: raise RuntimeError("桥接 DLL 未正确加载")
        report_path = ROOT / f".runtime/native_requests/{uuid.uuid4().hex}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if len(str(report_path)) >= 260: raise RuntimeError("报告路径超过原生接口长度")
        config = AtTextConfig() if mentions is not None else (
            ImageDownloadConfig() if download_image is not None else
            (TextConfig() if generic else Config()))
        config.size, config.pid, config.manager = c.sizeof(config), pid, 0
        config.log_path = str(report_path)
        if download_image is not None:
            config.text = destination.encode("utf-8")
            config.filekey = filekey.encode("ascii")
            config.aeskey = aeskey.encode("ascii")
            config.image_type = image_type
        else:
            config.text = encoded
        if generic:
            config.target = target.encode("ascii")
        if mentions is not None:
            config.members = ",".join(mentions).encode("ascii")
        config_parameter = allocate(bytes(config))
        exit_code = remote_call(process, loaded[0] + export, config_parameter)
        if mode == "--remove" and exit_code == 0:
            remote_call(process, remote_system_function(pid, b"FreeLibrary"), loaded[0])
        # Failed initialization or a partially scheduled job must still pass
        # the DLL's cleanup guard before FreeLibrary is allowed.
        result = {"mode":mode,"pid":pid,"code":exit_code,"target":target,"report_path":str(report_path)}
        if mode == "--remove":
            result["dll_unloaded"] = dll.name.lower() not in modules(pid)
        if mode in ("--dispatch", "--download-image"):
            deadline = time.monotonic() + 15
            while exit_code == 0 and not report_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if report_path.exists():
                # The completion closes the file before its callback lease is
                # released. Cleanup below waits for that lease before unload.
                for attempt in range(20):
                    try:
                        result["report"] = json.loads(report_path.read_text(encoding="utf-8")); break
                    except (json.JSONDecodeError, PermissionError): time.sleep(0.05)
            result["report_path"] = str(report_path)
            cleanup_name = b"WxWaitForTasks" if keep_loaded else b"WxRemoveSelfTest"
            cleanup_export = next(item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name == cleanup_name)
            cleanup_code = remote_call(process, loaded[0] + cleanup_export, None)
            result["cleanup_code"] = cleanup_code
            if cleanup_code == 0 and not keep_loaded:
                remote_call(process, remote_system_function(pid, b"FreeLibrary"), loaded[0])
                result["dll_unloaded"] = dll.name.lower() not in modules(pid)
            else:
                result["dll_unloaded"] = False
            report = result.get("report", {})
            result["success"] = (exit_code == 0 and report.get("completion_success") is True
                                 and report.get("target") == target and report.get("text") == text
                                 and cleanup_code == 0 and (keep_loaded or result.get("dll_unloaded") is True))
            if download_image is not None:
                report = result.get("report", {})
                result["success"] = (exit_code == 0 and report.get("completion_success") is True
                                     and report.get("operation") == "image_download"
                                     and report.get("image_path") == destination
                                     and type(report.get("bytes_written")) is int
                                     and report["bytes_written"] > 0
                                     and cleanup_code == 0
                                     and (keep_loaded or result.get("dll_unloaded") is True))
            if mentions is not None:
                result["success"] = result["success"] and result["report"].get("at_members") == ",".join(mentions)
            if not preserve_report and result["success"]:
                # 长期运行只保留内存中的完成结果；已结束的临时报告归档而不是删除。
                try:
                    archive = report_path.parent / "done"
                    archive.mkdir(parents=True, exist_ok=True)
                    report_path.replace(archive / report_path.name)
                    result["report_retained"] = False
                except OSError as error:
                    # 临时文件被占用不应把已经成功的发送变成失败。
                    result["report_retained"] = True
                    result["report_cleanup_error"] = str(error)
        return result
    except TimeoutError:
        # A timed-out remote thread may still be using these buffers. Keep them
        # allocated in the client until it exits instead of freeing live input.
        pending_thread = True
        raise
    finally:
        if not pending_thread:
            for pointer in allocations: K.VirtualFreeEx(process, pointer, 0, 0x8000)
        K.CloseHandle(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过微信原生任务队列给文件传输助手发送文本")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--arm", action="store_true")
    action.add_argument("--dispatch", action="store_true")
    action.add_argument("--remove", action="store_true")
    action.add_argument("--receive", action="store_true")
    action.add_argument("--listen", action="store_true", help="启用文件传输助手接收队列，结束后需 --remove")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--text", default="WXNATIVE_TEST_3")
    options = parser.parse_args()
    mode = "--arm" if options.arm else "--dispatch" if options.dispatch else "--receive" if options.receive else "--listen" if options.listen else "--remove"
    outcome = run(mode, pid=options.pid, text=options.text)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    sys.exit(0 if outcome.get("success", outcome["code"] == 0) else 1)
