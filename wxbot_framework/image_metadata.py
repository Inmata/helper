"""图片 XML 元数据与本地图片保存；不下载 CDN 内容或读取微信文件。

与上游实现的差异：本模块不物理删除任何文件。保存失败时保留临时文件供人工清理，
成功时用 ``os.replace`` 原子提交；拒绝通过 XML、URL 或扩展名决定路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import xml.etree.ElementTree as ET

from .account import validate_account
from .images import MAX_IMAGE_BYTES


MAX_IMAGE_XML_BYTES = 16_384
MAX_IMAGE_DIMENSION = 65_535
MAX_METADATA_NUMBER = (1 << 31) - 1
_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_DECIMAL = re.compile(r"[0-9]{1,10}")


class ImageMetadataError(ValueError):
    """XML 无效或图片属性超出支持范围；错误信息不包含原始 XML。"""


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """仅保存已校验属性；URL、密钥和 filekey 不出现在默认 repr 中。

    缺失字符串为 ""，缺失数值为 None。元数据不意味着图片已下载、
    已解密或存在于本地。数值 0 保留微信原始含义，不推断真实尺寸。
    """

    md5: str = ""
    width: int | None = None
    height: int | None = None
    length: int | None = None
    hdlength: int | None = None
    cdnthumbwidth: int | None = None
    cdnthumbheight: int | None = None
    cdnthumblength: int | None = None
    cdnmidwidth: int | None = None
    cdnmidheight: int | None = None
    encryver: int | None = None
    cdnthumburl: str = field(default="", repr=False)
    cdnmidimgurl: str = field(default="", repr=False)
    cdnbigimgurl: str = field(default="", repr=False)
    aeskey: str = field(default="", repr=False)
    cdnthumbaeskey: str = field(default="", repr=False)
    filekey: str = field(default="", repr=False)


def _number(attributes, name, maximum):
    if name not in attributes:
        return None
    value = attributes[name]
    if not _DECIMAL.fullmatch(value) or int(value) > maximum:
        raise ImageMetadataError(f"图片属性 {name} 必须为范围内的非负整数")
    return int(value)


def _opaque(attributes, name, maximum):
    value = attributes.get(name, "")
    if len(value.encode("utf-8")) > maximum or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ImageMetadataError(f"图片属性 {name} 过长或包含控制字符")
    return value


def parse_image_metadata(content: str) -> ImageMetadata:
    """解析一个 msg/img XML，失败抛 ImageMetadataError。

    群成员前缀应由会话路由先行剥离。本函数不猜测前缀，不解析 DTD、
    自定义实体或本地文件引用；标准 XML 转义（如 &amp;）仍可正常解析。
    """
    if not isinstance(content, str):
        raise ImageMetadataError("图片 XML 必须是非空字符串")
    if len(content) > MAX_IMAGE_XML_BYTES:
        raise ImageMetadataError("图片 XML 超过 16384 字节上限")
    if not content.strip():
        raise ImageMetadataError("图片 XML 必须是非空字符串")
    if "\0" in content:
        raise ImageMetadataError("图片 XML 不能包含 NUL")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeError:
        raise ImageMetadataError("图片 XML 必须包含有效的 UTF-8 字符") from None
    if size > MAX_IMAGE_XML_BYTES:
        raise ImageMetadataError("图片 XML 超过 16384 字节上限")
    if _FORBIDDEN_DECLARATION.search(content):
        raise ImageMetadataError("图片 XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise ImageMetadataError("图片 XML 格式不合法") from None
    if root.tag != "msg":
        raise ImageMetadataError("图片 XML 必须具有 msg/img 结构")
    images = list(root.iter("img"))
    if len(images) != 1 or images[0] not in list(root):
        raise ImageMetadataError("图片 XML 必须恰好包含一个直接的 img 子元素")
    # 输入已经有字节上限，再限制结构以拒绝无用的深层嵌套。
    pending, count = [(root, 1)], 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 128 or depth > 16:
            raise ImageMetadataError("图片 XML 结构过于复杂")
        pending.extend((child, depth + 1) for child in element)
    attributes = images[0].attrib
    if len(attributes) > 64:
        raise ImageMetadataError("图片 XML 属性过多")
    md5 = attributes.get("md5", "")
    if "md5" in attributes and not _MD5.fullmatch(md5):
        raise ImageMetadataError("图片 md5 必须为 32 位十六进制字符串")
    numbers = {
        name: _number(attributes, name, MAX_IMAGE_DIMENSION)
        for name in ("width", "height", "cdnthumbwidth", "cdnthumbheight", "cdnmidwidth", "cdnmidheight")
    }
    numbers.update({
        name: _number(attributes, name, MAX_METADATA_NUMBER)
        for name in ("length", "hdlength", "cdnthumblength", "encryver")
    })
    opaque = {
        name: _opaque(attributes, name, 8192)
        for name in ("cdnthumburl", "cdnmidimgurl", "cdnbigimgurl")
    }
    opaque.update({
        name: _opaque(attributes, name, 1024)
        for name in ("aeskey", "cdnthumbaeskey", "filekey")
    })
    return ImageMetadata(md5=md5.lower(), **numbers, **opaque)


def _image_extension(data):
    # 与发送层一致，依据字节签名识别格式，不把 .dat 扩展名或 XML 当成图片。
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    raise ValueError("图片格式仅支持 JPEG、PNG、GIF、WebP 和 BMP；加密数据必须先解密")


class ImageStore:
    """将调用方已经持有的图片字节原子保存到指定账号目录。

    账号目录使用完整 SHA-256，避免路径穿越、Windows 大小写和保留名称冲突。
    XML、远程 URL、文件扩展名都不能决定输出目录或文件名。
    """

    def __init__(self, root: str | os.PathLike[str], account: str):
        validate_account(account)
        try:
            value = os.fspath(root)
        except TypeError:
            raise ValueError("图片保存目录必须为本地路径") from None
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("图片保存目录必须为非空本地路径")
        self.root = Path(value).expanduser().resolve()
        self.account = account
        namespace = hashlib.sha256(account.encode("ascii")).hexdigest()
        self.directory = self.root / ("account-" + namespace)

    def _prepare_directory(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(exist_ok=True)
        if self.directory.resolve(strict=True) != self.directory or not self.directory.is_dir():
            raise ValueError("图片账号目录不能通过链接重定向")

    def _reserve_destination(self, extension, sequence):
        prefix = "image-" if sequence is None else f"image-{sequence}-"
        for _ in range(32):
            destination = self.directory / (prefix + secrets.token_hex(16) + extension)
            if not destination.exists() and not destination.is_symlink():
                return destination
        raise FileExistsError("无法生成不重复的图片文件名")

    def save(self, data: bytes, *, sequence: int | None = None) -> Path:
        """保存非空且最多 20 MiB 的图片字节，返回已提交的绝对路径。

        sequence 仅用于诊断文件名，不是唯一键；同一条消息重复保存也不会覆盖。
        校验失败抛 ValueError，写入失败保留 OSError；失败时临时文件保留在原处，
        按项目约定不执行物理删除。
        """
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise ValueError("图片数据必须为非空 bytes，且不能超过 20 MiB")
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= (1 << 64) - 1):
            raise ValueError("图片消息序号必须为 0 到 2^64-1 的整数")
        extension = _image_extension(data)
        self._prepare_directory()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=self.directory)
        temporary = Path(temporary_name)
        try:
            try:
                target = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            destination = self._reserve_destination(extension, sequence)
            # 文件内容先写完并关闭；提交时才出现正式图片路径。
            os.replace(temporary, destination)
            return destination
        except BaseException:
            # 不删除半成品；保留可诊断的临时文件。
            raise

    def commit_file(self, source: str | os.PathLike[str], *, sequence: int | None = None) -> Path:
        """把已下载且校验通过的临时文件移到账号目录，返回已提交的绝对路径。

        只做移动，不复制或删除内容；临时文件校验失败时保留原处。
        """
        path = Path(source)
        if not path.is_absolute() or path.parent != self.directory:
            raise ValueError("图片下载临时文件必须位于当前账号目录")
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= (1 << 64) - 1):
            raise ValueError("图片消息序号必须为 0 到 2^64-1 的整数")
        self._prepare_directory()
        try:
            information = path.lstat()
        except OSError as error:
            raise ValueError("图片下载结果不存在") from error
        if (not stat.S_ISREG(information.st_mode) or path.resolve(strict=True) != path or
                not 0 < information.st_size <= MAX_IMAGE_BYTES):
            raise ValueError("图片下载结果必须为范围内的普通文件")
        with path.open("rb") as source_file:
            header = source_file.read(16)
            opened = os.fstat(source_file.fileno())
            if (not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_IMAGE_BYTES):
                raise ValueError("图片下载结果必须为范围内的普通文件")
        extension = _image_extension(header)
        destination = self._reserve_destination(extension, sequence)
        os.replace(path, destination)
        return destination
