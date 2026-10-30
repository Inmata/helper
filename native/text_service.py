"""好友、群聊和文件传输助手的原生文本服务。"""
from __future__ import annotations

import logging
import threading
import time
import ctypes as c
from collections import deque
from pathlib import Path

from filehelper_service import (NativeFileHelperService, ReceivedTextMessage, ReceivedTextMessageV2,
                                ReceivedMediaMessageV4)
from wxbot_framework.image_receiver import ImageDownloadSkipped
from wxbot_framework.image_metadata import parse_image_metadata
from wxbot_framework.recent_images import RECENT_IMAGES
from wxbot_framework.messaging.mentions import parse_at_members, parse_quote
from run_selftest import TEXT_DLL, K, check, download_aeskey_supported, modules, wechat_candidates, run
from wxbot_framework.messaging.text import TextRouter, validate_target, validate_mentions
from wxbot_framework.account import read_current_account, read_current_profile, select_account_process, validate_account
from wxbot_framework.contacts import read_chatroom_members, read_contact_name

logger = logging.getLogger("NativeText")

# 已编译的桥接可能是旧协议：V2 正文只有 1KB，V3 扩到 8KB，V4 再带图片 XML
# 与 64 位原始消息编号。按实际导出选择接收约定，避免新代码直接对着旧 DLL 启动失败。
V4_EXPORT = b"WxPollMediaMessageV4"
V3_EXPORT = b"WxPollTextMessageV3"
V2_EXPORT = b"WxPollTextMessageV2"


def bridge_exports():
    import pefile
    with pefile.PE(str(TEXT_DLL)) as pe:
        return {item.name for item in pe.DIRECTORY_ENTRY_EXPORT.symbols}


def text_message_binding(exports=None):
    """返回 (轮询导出名, 接收结构)，优先选择能力最完整且已编译的协议。"""
    try:
        exports = exports if exports is not None else bridge_exports()
    except Exception:  # 读不到 PE 时按最保守的旧协议走，启动阶段另有模块检查兜底
        return V2_EXPORT, ReceivedTextMessageV2
    if V4_EXPORT in exports:
        return V4_EXPORT, ReceivedMediaMessageV4
    if V3_EXPORT in exports:
        return V3_EXPORT, ReceivedTextMessage
    return V2_EXPORT, ReceivedTextMessageV2


def run_text(mode, **options):
    return run(mode, bridge_path=TEXT_DLL, **options)


def inspect_account(pid):
    """只读核对候选进程的当前账号，不向候选进程注入 DLL。"""
    core = modules(pid).get("weixin.dll")
    if not core:
        return None
    process = check(K.OpenProcess(0x410, False, pid), "只读核对微信账号")
    try:
        def read(address, size):
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), "只读核对账号字段")
            return buffer.raw[:count.value]
        return read_current_account(read, core[0])
    finally:
        K.CloseHandle(process)


def find_account_process(account=None):
    candidates = []
    for pid in wechat_candidates():
        try:
            candidates.append((pid, inspect_account(pid)))
        except OSError:
            # 枚举后退出的进程不参与选择。
            pass
    return select_account_process(candidates, account)


class NativeTextService(NativeFileHelperService):
    _dll = TEXT_DLL
    _poll_export = V2_EXPORT
    _message_type = ReceivedTextMessageV2
    _run = staticmethod(run_text)

    def __init__(self, *, account=None, media_directory=None, image_allowlist=(), **options):
        super().__init__(**options)
        # 启动前按已编译 DLL 的实际导出选定接收协议；V4 未编译时退回 V3/V2。
        self._poll_export, self._message_type = text_message_binding()
        if self._poll_export == V4_EXPORT:
            logger.info("桥接为 V4 协议：正文上限 8KB，接收引用消息与图片")
        elif self._poll_export == V3_EXPORT:
            logger.info("桥接为 V3 协议：正文上限 8KB，接收引用消息；图片接收未编译")
        else:
            logger.info("桥接为 V2 协议：正文上限 1KB，引用与图片暂不接收；重编桥接后自动启用")
        self.preferred_account = validate_account(account) if account is not None else None
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._next_account_check = 0
        self._sent_texts = deque(maxlen=64)
        self.mention_messages = 0
        self.image_messages = 0
        self.media_directory = Path(media_directory or Path(__file__).resolve().parent.parent / ".runtime/images")
        self.image_allowlist = frozenset(image_allowlist)
        self._image_receiver = None
        self._image_receiver_lock = threading.Lock()

    @property
    def self_wxid(self):
        return self.router.self_wxid

    def _ensure_image_receiver(self):
        """账号识别后按账号创建异步图片接收器；账号隔离由 ImageStore 的目录哈希保证。"""
        if self._image_receiver is not None:
            return self._image_receiver
        account = self.self_wxid
        if not account:
            return None
        with self._image_receiver_lock:
            if self._image_receiver is None:
                from wxbot_framework.image_metadata import ImageStore
                from wxbot_framework.image_receiver import ImageReceiver
                self._image_receiver = ImageReceiver(
                    ImageStore(self.media_directory, account), self._download_image, self._deliver_image,
                    lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)
        return self._image_receiver

    def _download_image(self, metadata, destination):
        """在当前账号仍有效时调用原生 CDN 下载；前后各核对一次账号。

        原生接口只接受 32 或 64 位十六进制密钥，而只带缩略图的 XML 提供的是
        16 位 ``cdnthumbaeskey``；上游同样没有可用的转换依据，因此不做猜测性转换，
        统一以 skipped 事件说明原因，避免含糊的 failed。
        """
        filekey = metadata.cdnmidimgurl or metadata.filekey or metadata.cdnbigimgurl
        if not filekey:
            raise ImageDownloadSkipped("仅提供缩略图引用，缩略图密钥不受原生下载接口支持"
                                       if metadata.cdnthumburl else "图片元数据没有可下载地址")
        if not metadata.aeskey:
            raise ImageDownloadSkipped("图片元数据没有可下载密钥")
        if not download_aeskey_supported(metadata.aeskey):
            raise ImageDownloadSkipped("图片密钥格式不受原生下载接口支持")
        image_type = 2 if (metadata.cdnmidimgurl or metadata.filekey) else 1
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                return False
            self._check_account(force=True)
            result = self._run("--download-image", pid=self.pid, keep_loaded=True,
                               preserve_report=self.preserve_reports,
                               download_image={"destination": str(destination), "filekey": filekey,
                                               "aeskey": metadata.aeskey, "image_type": image_type})
            self._check_account(force=True)
            return result.get("success") is True

    def _deliver_image(self, data):
        if self._stop.is_set():
            return
        # 只记录会话和状态；本机路径、XML、URL 与密钥不写日志。
        logger.info("图片接收 | 会话=%s | 状态=%s", data.get("chat_id"), data.get("image_status"))
        RECENT_IMAGES.note(data)
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception as error:
                # 异常文本可能带图片路径，只记类型。
                logger.warning("图片消息处理器异常：%s", type(error).__name__)

    def start(self):
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._sent_texts.clear()
        self.image_messages = 0
        # 重连后消息序号重新计数，旧索引里的序号键可能撞上新消息。
        RECENT_IMAGES.clear()
        if self.pid is None:
            self.pid = find_account_process(self.preferred_account)
        elif self.preferred_account and inspect_account(self.pid) != self.preferred_account:
            # 手动指定 PID 时也先只读核对，避免向其他账号加载 DLL。
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        result = super().start()
        logger.info("当前账号已自动识别：%s；好友和群聊文本已就绪", self.self_wxid)
        return result

    def stop(self):
        self._stop.set()
        if self._image_receiver is not None:
            self._image_receiver.close(timeout=30)
            if self._image_receiver.is_alive:
                # 下载器尚未返回时不能卸载桥接；保留实例等待下一轮清理。
                raise RuntimeError("图片原生下载尚未结束，保留桥接等待清理重试")
            self._image_receiver = None
        return super().stop()

    def _read_account(self):
        return read_current_account(self._channel.read_memory, self._channel.core_base)

    def _prepare_channel(self):
        account = self._read_account()
        if not account:
            raise RuntimeError("微信尚未完成登录，自动等待当前账号就绪")
        if self.preferred_account and account != self.preferred_account:
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        self.router.set_account(account)
        self._next_account_check = time.monotonic() + 1

    def _check_account(self, *, force=False):
        if not force and time.monotonic() < self._next_account_check:
            return
        account = self._read_account()
        if not account or account != self.self_wxid:
            error = RuntimeError("微信已退出登录或当前账号发生变化，重新连接并自动识别")
            self.last_error = error
            self._stop.set()
            raise error
        self._next_account_check = time.monotonic() + 1

    def _before_poll(self):
        self._check_account()

    def _sender_wechat_name(self, wxid):
        if self._channel is None:
            return None
        name = read_contact_name(self._channel.read_memory, self._channel.core_base, wxid,
                                 expected_account=self.self_wxid)
        if name is None and wxid == self.self_wxid:
            profile = read_current_profile(self._channel.read_memory, self._channel.core_base)
            name = profile["name"] if profile and profile["wxid"] == wxid else None
        return name

    def read_room_directory(self, room_wxid):
        if self._channel is None:
            return (), {}
        members = read_chatroom_members(self._channel.read_memory, self._channel.core_base, room_wxid,
                                        expected_account=self.self_wxid)
        names = {}
        for wxid in members:
            name = self._sender_wechat_name(wxid)
            if name:
                names[wxid] = name
        return members, names

    def _map_message(self, message):
        # 1 是文本；3 是图片；49 是应用消息，只有引用（refermsg）才当文本处理，其余卡片丢弃。
        if message.type not in (1, 3, 49):
            return None
        body = (getattr(message, "image_xml", b"") if message.type == 3 else
                getattr(message, "text", b"")).decode("utf-8", errors="replace")
        quote = None
        if message.type == 49:
            quote = parse_quote(body)
            if quote is None:
                return None
        previous = self.self_wxid
        previous_unparsed = self.router.unparsed_groups
        data = self.router.convert(message.sender.decode("utf-8"), message.receiver.decode("utf-8"), body,
                                   sequence=message.sequence, thread_id=message.thread_id)
        if self.self_wxid and not previous:
            logger.info("当前账号已识别；好友和群聊文本已就绪")
        if data:
            data["native_message_id"] = str(getattr(message, "native_message_id", 0) or "")
            if quote is not None:
                # 回复正文在被引用卡片里，群友真正说的话是 title；被引用的原文单独带给模型。
                data["quote"] = {key: quote[key] for key in ("name", "text", "chatusr", "fromusr", "ref_type",
                                                             "svrid", "image_md5")}
                data["msg"] = data["content"] = quote["title"]
            name = self._sender_wechat_name(data["sender_id"]) if data["sender_id"] else None
            if name:
                data["sender_wechat_name"] = name
            source = getattr(message, "mention_source", b"").decode("utf-8", errors="replace")
            data["at_members"] = parse_at_members(source)
            if data["at_members"]:
                at_names = {}
                for wxid in data["at_members"]:
                    at_name = self._sender_wechat_name(wxid)
                    if at_name:
                        at_names[wxid] = at_name
                if at_names:
                    data["at_member_names"] = at_names
            data["is_at_self"] = bool(data["room_wxid"] and self.self_wxid in data["at_members"])
            if data["at_members"]:
                self.mention_messages += 1
            if quote is not None:
                # 引用图片时原文里有 aeskey 和 CDN 地址，只记结构字段，不记原始 XML。
                logger.info("引用消息：room=%s sender=%s ref_type=%s name=%s svrid=%s is_at_self=%s at=%s",
                            data["chat_id"], data["sender_id"], quote["ref_type"], quote["name"],
                            quote["svrid"], data["is_at_self"], data["at_members"])
            if data["is_self"] and any(chat == data["chat_id"] and text == data["msg"] and
                                       time.monotonic() - sent_at < 120
                                       for chat, text, sent_at in tuple(self._sent_texts)):
                self.router.ignored_self += 1
                data["bot_generated"] = True
                data["dispatch_to_plugins"] = False
            elif data["is_at_self"]:
                data["dispatch_to_plugins"] = True
            if "WXBOT_AT_CHAT_PROBE" in data["msg"]:
                import json
                from pathlib import Path
                path = Path(__file__).resolve().parents[1] / ".runtime/at_receive_probe.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"room": data["chat_id"], "sender": data["sender_id"],
                                            "at_members": data["at_members"], "is_at_self": data["is_at_self"],
                                            "source": source}, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("艾特接收探针：at_members=%s is_at_self=%s", data["at_members"], data["is_at_self"])
            self.chats[data["chat_id"]] = {"chat_id": data["chat_id"],
                                           "is_group": bool(data["room_wxid"])}
            if data["msg"].startswith(("WXBOT_", "echo WXBOT_")):
                logger.info("测试消息已接收：chat_id=%s sender_id=%s group=%s marker=%s",
                            data["chat_id"], data["sender_id"], bool(data["room_wxid"]), data["msg"])
            if message.type == 3:
                return self._map_image(data, body)
        elif self.router.unparsed_groups > previous_unparsed:
            # 第一条失败时提示；后续通过 status 查看累计数。
            if self.router.unparsed_groups == 1:
                logger.warning("群文本作者格式未匹配，已跳过；需要用测试群消息核对实际布局")
        return data

    def _map_image(self, data, body):
        """图片事件进入异步队列；下载完成后才以 type=3 投递给插件。"""
        # router.convert 已剥离群成员前缀。raw_content 只是交给接收器解析的内部字段，
        # ImageReceiver 在解析后立即移除，插件事件里不会出现 XML、CDN 地址或密钥。
        data["raw_content"] = data.get("msg") or body
        data["wx_type"] = 3
        data["msg"] = data["content"] = ""
        data["image_status"] = "pending"
        data["image_path"] = ""
        data["image_error"] = ""
        data["image_metadata"] = {}
        data["image_quality"] = "unknown"
        self.image_messages += 1
        enabled = not self.image_allowlist or data["chat_id"] in self.image_allowlist
        if not enabled:
            # 会话不在图片名单：不下载，也不把内部 XML 交给插件；按规范投递 skipped 说明原因。
            data["image_status"] = "skipped"
            data["image_error"] = "会话不在图片名单"
            for field in ("raw_content", "content", "msg"):
                data[field] = ""
            RECENT_IMAGES.note(data)
            return data
        receiver = self._ensure_image_receiver()
        if receiver is None:
            # 账号尚未识别时不应出现图片事件；给出可观察的失败而不是把 XML 当正文。
            data.update(image_status="failed", image_error="图片接收器未就绪")
            for field in ("raw_content", "content", "msg"):
                data[field] = ""
            RECENT_IMAGES.note(data)
            return data
        # 先记 pending：群友发图后立刻艾特时，回复方能知道图还在下载并稍等。
        # 公开的 md5 也先写上，引用对不上消息编号时仍能按 md5 找到下载中的这张。
        pending = {key: data.get(key) for key in ("chat_id", "sender_id", "native_message_id",
                                                  "sequence", "image_status")}
        try:
            pending["image_metadata"] = {"md5": parse_image_metadata(data["raw_content"]).md5}
        except Exception:
            pass  # XML 有问题时接收器会给出 failed，这里不提前判断
        RECENT_IMAGES.note(pending)
        receiver.submit(dict(data))
        return None

    def send_text(self, to_wxid, content):
        return self._send_text(to_wxid, content)

    def send_at_text(self, room_wxid, content, at_list):
        members = validate_mentions(room_wxid, at_list)
        return self._send_text(room_wxid, content, mentions=members)

    def _send_text(self, to_wxid, content, *, mentions=None):
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文本服务没有运行")
            self._check_account(force=True)
            self._sent_texts.append((to_wxid, content, time.monotonic()))
            self.last_result = self._run("--dispatch", pid=self.pid, text=content, target=to_wxid,
                                        keep_loaded=True, preserve_report=self.preserve_reports, mentions=mentions)
            if content.startswith("WXBOT_"):
                logger.info("测试文本发送完成：chat_id=%s marker=%s success=%s native_thread=%s",
                            to_wxid, content, self.last_result["success"],
                            self.last_result.get("report", {}).get("thread_id"))
            return self.last_result["success"]
