"""原生收发后端；框架和插件只依赖公开接口。"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from ..messaging.message import Message

CAPABILITIES = frozenset({"text.receive", "text.send"})
TEXT_CAPABILITIES_BASE = CAPABILITIES | frozenset({"friend.text.receive", "friend.text.send",
                                                   "group.text.receive", "group.text.send",
                                                   "group.text.mention"})
IMAGE_CAPABILITIES = frozenset({"image.receive", "friend.image.receive", "group.image.receive"})
# 图片事件依赖 V4 媒体队列，下载依赖原生 CDN 接口；两者都编译进当前 DLL 才声明能力。
IMAGE_RECEIVE_EXPORT = b"WxPollMediaMessageV4"
IMAGE_DOWNLOAD_EXPORT = b"WxDownloadImage"
TEXT_DLL = Path(__file__).resolve().parents[2] / "native/build_text/text_bridge.dll"


def compiled_bridge_exports(path=None):
    """只读查看已编译桥接的导出；文件缺失或无法解析时按没有图片能力处理。"""
    try:
        import pefile
        with pefile.PE(str(path or TEXT_DLL)) as pe:
            return frozenset(item.name for item in pe.DIRECTORY_ENTRY_EXPORT.symbols)
    except Exception:
        return frozenset()


def text_capabilities(exports=None):
    """按实际选中的接收协议声明能力。

    旧 V3/V2 桥接没有图片队列和下载接口，不能只凭配置声明图片能力，
    否则依赖图片的插件会通过检查却始终收不到图。
    """
    exports = compiled_bridge_exports() if exports is None else frozenset(exports)
    if {IMAGE_RECEIVE_EXPORT, IMAGE_DOWNLOAD_EXPORT} <= exports:
        return TEXT_CAPABILITIES_BASE | IMAGE_CAPABILITIES
    return TEXT_CAPABILITIES_BASE


def text_scope(capabilities=None):
    """按实际声明的能力描述收发范围；没有图片能力时不能写“群图片”。"""
    capabilities = TEXT_CAPABILITIES if capabilities is None else frozenset(capabilities)
    scope = "文件传输助手、好友和群聊文本"
    if IMAGE_CAPABILITIES <= capabilities:
        scope += "及群图片"
    return scope


# 启动时按当前 DLL 的实际导出计算；重编桥接后需重启机器人重新探测。
TEXT_CAPABILITIES = text_capabilities()


class NativeFileHelperBackend:
    scope = "文件传输助手文本"
    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到已验证的原生适配目录")
        # 后端模块按需导入；查看插件和检查配置不需要打开微信。
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from filehelper_service import NativeFileHelperService
        self.raw = NativeFileHelperService(pid=config.get("wechat_pid"), preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def start(self, emit):
        self.raw.register_msg_handler(lambda _client, protocol, data, _service:
                                      emit(Message.from_native(protocol, data, self.session_id)))
        return self.raw.start()

    @property
    def last_error(self):
        return self.raw.last_error

    @property
    def pid(self):
        return self.raw.pid

    def send_text(self, chat_id, text):
        return self.raw.send_text(chat_id, text)

    def stop(self):
        return self.raw.stop()


class NativeTextBackend(NativeFileHelperBackend):
    @property
    def scope(self):
        # 连接日志会展示该范围；必须与后端实际声明的图片能力一致。
        return text_scope()

    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到原生适配目录")
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        # 图片目录可按配置覆盖；未配置图片名单时沿用群收发名单，避免下载名单外的图片。
        image_allowlist = config.get("image_allowlist", config.get("chat_allowlist", ()))
        self.raw = NativeTextService(pid=config.get("wechat_pid"), account=config.get("wechat_account"),
                                     media_directory=config.get("image_directory"),
                                     image_allowlist=image_allowlist or (),
                                     preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def diagnostics(self):
        router = self.raw.router
        return {"account_ready": bool(self.raw.self_wxid),
                "account_source": "current_process",
                "self_wxid": self.raw.self_wxid,
                "bound_account": self.raw.preferred_account,
                "unidentified_messages": router.unidentified,
                "unparsed_groups": router.unparsed_groups,
                "ignored_self_messages": router.ignored_self,
                "mention_messages": self.raw.mention_messages,
                "image_messages": self.raw.image_messages}

    def chats(self):
        return list(self.raw.chats.values())

    def send_at_text(self, room_wxid, content, at_list):
        return self.raw.send_at_text(room_wxid, content, at_list)

    def read_room_directory(self, room_wxid):
        return self.raw.read_room_directory(room_wxid)
