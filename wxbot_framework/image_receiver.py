"""受控图片接收队列；下载能力由调用方提供，本模块不连接微信或网络。

与上游实现的差异：不创建也不删除临时目录。下载器写入账号目录内唯一且
尚不存在的 ``.receive-*`` 临时文件；成功后由 :class:`ImageStore` 移到正式路径，
失败时保留临时文件供人工清理（项目约定禁止物理删除）。
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import queue
import secrets
import threading

from .image_metadata import ImageMetadata, ImageMetadataError, ImageStore, parse_image_metadata
from .images import MAX_IMAGE_BYTES


_PUBLIC_FIELDS = (
    "md5", "width", "height", "length", "hdlength", "cdnthumbwidth", "cdnthumbheight",
    "cdnthumblength", "cdnmidwidth", "cdnmidheight", "encryver",
)


class ImageDownloadSkipped(Exception):
    """下载器声明该图片不在支持范围；原因会作为 ``image_error`` 投递给插件。

    原因必须是可公开的简短文本，不能包含 XML、CDN 地址或密钥。
    """


class ImageReceiver:
    """异步下载和保存图片，每个接收器只有一个下载工作线程。

    downloader(metadata, destination) 应在返回前关闭输出文件，成功返回 True；
    若该图片不在支持范围，可抛 :class:`ImageDownloadSkipped` 说明原因。
    destination 是账号目录内尚不存在的临时文件，不能由 XML 内容决定。
    只有当前会话会收到 ready/failed/skipped 事件；原始图片 XML 在解析后从事件中移除。
    """

    def __init__(self, store: ImageStore, downloader, deliver, is_current, queue_size=16):
        if not isinstance(store, ImageStore):
            raise ValueError("图片接收器需要 ImageStore")
        if not all(callable(value) for value in (downloader, deliver, is_current)):
            raise ValueError("图片下载、投递和会话检查必须为可调用对象")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("图片接收队列容量必须为 1 到 1024 的整数")
        self.store = store
        self._root = Path(store.root)
        self._directory = Path(store.directory)
        if not self._root.is_absolute() or self._directory.parent != self._root:
            raise ValueError("图片账号目录必须是保存根目录的直接子目录")
        self._downloader = downloader
        self._deliver_callback = deliver
        self._is_current = is_current
        self._queue = queue.Queue(maxsize=queue_size)
        self._enqueue_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="WechatImageReceiver", daemon=True)
        self._worker.start()

    @property
    def is_alive(self):
        """关闭超时后仍可能在等待 downloader；原生资源此时仍不能卸载。"""
        return self._worker.is_alive()

    def _active(self):
        if self._stop.is_set():
            return False
        try:
            current = bool(self._is_current())
        except Exception:
            return False
        return current and not self._stop.is_set()

    def _deliver(self, data):
        if self._active():
            try:
                self._deliver_callback(data)
            except Exception:
                # 回调错误不能让下载线程退出，也不能把带秘密的异常写入日志。
                return False
            return True
        return False

    def _failed(self, data, message):
        data.update(image_status="failed", image_path="", image_error=message)
        self._deliver(data)

    def submit(self, data: dict) -> bool:
        """非阻塞入队；元数据无效或队列已满时投递可观察失败并返回 False。"""
        if not isinstance(data, dict):
            raise ValueError("图片事件必须为字典")
        if not self._active():
            return False
        snapshot = dict(data)
        snapshot.update(image_path="", image_error="", image_metadata={}, image_quality="unknown")
        # 原始 XML 含 CDN 地址和密钥，只用于本次解析；任何投递路径都不再带上它。
        raw_content = snapshot.pop("raw_content", "")
        try:
            metadata = parse_image_metadata(raw_content)
        except ImageMetadataError:
            self._failed(snapshot, "图片元数据无效")
            return False
        snapshot["image_metadata"] = {name: getattr(metadata, name) for name in _PUBLIC_FIELDS}
        snapshot["image_quality"] = ("standard" if metadata.cdnmidimgurl or metadata.filekey else
                                     "original" if metadata.cdnbigimgurl else
                                     "thumbnail" if metadata.cdnthumburl else "unknown")
        full = False
        with self._enqueue_lock:
            if self._stop.is_set():
                return False
            try:
                self._queue.put_nowait((snapshot, metadata))
            except queue.Full:
                full = True
        if full:
            self._failed(snapshot, "图片接收队列已满")
            return False
        return True

    def _new_download_path(self):
        # 下载目标由本地随机数决定；先确认账号目录没有通过链接重定向。
        self._root.mkdir(parents=True, exist_ok=True)
        self._directory.mkdir(exist_ok=True)
        if (self._root.resolve(strict=True) != self._root or
                self._directory.resolve(strict=True) != self._directory or
                not self._directory.is_dir()):
            raise ValueError("图片账号目录不能通过链接重定向")
        for _ in range(32):
            destination = self._directory / (".receive-" + secrets.token_hex(16) + ".part")
            if not destination.exists() and not destination.is_symlink():
                return destination
        raise FileExistsError("无法生成不重复的图片临时文件名")

    @staticmethod
    def _validate_download(destination: Path):
        information = destination.lstat()
        if (not stat_is_regular(information.st_mode) or destination.resolve(strict=True) != destination or
                not 0 < information.st_size <= MAX_IMAGE_BYTES):
            raise ValueError("图片下载结果必须为范围内的普通文件")
        with destination.open("rb") as source:
            opened = os.fstat(source.fileno())
            header = source.read(16)
        if (not stat_is_regular(opened.st_mode) or not 0 < opened.st_size <= MAX_IMAGE_BYTES or
                not header):
            raise ValueError("图片下载结果必须为非空且不超过 20 MiB 的普通文件")

    def _download(self, metadata: ImageMetadata):
        destination = self._new_download_path()
        if not self._active():
            return None
        if self._downloader(metadata, destination) is not True:
            raise ValueError("图片下载未成功")
        if not self._active():
            return None
        self._validate_download(destination)
        return destination

    def _process(self, data, metadata):
        if not self._active():
            return
        try:
            temporary = self._download(metadata)
            if temporary is None or not self._active():
                return
            path = self.store.commit_file(temporary, sequence=data.get("sequence"))
            if not self._active():
                # 会话已结束：保留已提交文件，不执行物理删除。
                return
            data.update(image_status="ready", image_path=str(path), image_error="")
            self._deliver(data)
        except ImageDownloadSkipped as error:
            # 不属于失败：图片不在当前接口的支持范围，保留可观察原因。
            data.update(image_status="skipped", image_path="",
                        image_error=str(error) or "图片不在支持范围")
            self._deliver(data)
        except Exception:
            # 下载器和文件系统异常可能包含 CDN URL、密钥或 XML，不能直接转发。
            self._failed(data, "图片下载或保存失败")

    def _run(self):
        while not self._stop.is_set():
            try:
                data, metadata = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._process(data, metadata)
            finally:
                self._queue.task_done()

    def close(self, timeout=1):
        """取消待处理事件，有限等待工作线程；下载器未返回时 is_alive 仍为 True。"""
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout < 0):
            raise ValueError("图片接收器关闭等待时间必须为有限非负数")
        self._stop.set()
        with self._enqueue_lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._queue.task_done()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=min(timeout, 60))


def stat_is_regular(mode):
    return (mode & 0o170000) == 0o100000
