"""测试专用临时目录：清理时移入回收站，绝不永久删除。"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import weakref
from ctypes import wintypes
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(path):
    """把文件或目录移入回收站；失败时抛出 OSError。"""
    target = Path(path)
    if not target.exists():
        return
    if not _IS_WINDOWS:
        raise OSError("当前平台没有回收站")
    source = str(target.resolve()) + "\0"
    operation = _SHFILEOPSTRUCTW(
        None,
        _FO_DELETE,
        source,
        None,
        _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI,
        False,
        None,
        None,
    )
    function = ctypes.windll.shell32.SHFileOperationW
    function.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    function.restype = ctypes.c_int
    result = function(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(result, "移入回收站失败", str(target))


class RecyclableTemporaryDirectory:
    """替代 tempfile.TemporaryDirectory；cleanup 只把目录移入回收站。"""

    def __init__(self, suffix=None, prefix=None, dir=None):
        self.name = tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
        self._finalizer = weakref.finalize(self, self._recycle, self.name)

    @staticmethod
    def _recycle(name):
        path = Path(name)
        if not path.exists():
            return
        try:
            move_to_recycle_bin(path)
        except OSError as error:
            # 不退回永久删除；保留目录供人工处理。
            print(f"临时目录未能移入回收站，已保留：{path}（{error}）", file=sys.stderr)

    def cleanup(self):
        self._finalizer()

    def __enter__(self):
        return self.name

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()
