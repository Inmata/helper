"""群聊语音回复与按成员保存的固定语音设置。"""
from random import random
from subprocess import SubprocessError
from time import monotonic
from threading import Lock

from wxbot_framework.conversation.personality import DEFAULT_VOICE, VOICE_FEATURE, room_voice, voice_self_name
from wxbot_framework.conversation.members import MemberError
from wxbot_framework.integrations.voice_synth import synthesize_voice
from wxbot_framework.integrations.voice_local import LocalVoice

COMMAND = "/音色"
READ_COMMAND = "/语音"
REPLY_COMMAND = "/语音回复"
REPLY_FEATURE = "mention_voice_reply"
REPLY_USAGE = "用法：/语音回复 <@群友|M序号|昵称|微信ID> [开启|关闭]；省略目标查看本群设置。"
_AUTO_CHANCE = 0.5
_INTERVAL_SECONDS = 5 * 60
_MAX_AUTO_CHARS = 40
_MAX_REQUEST_CHARS = 80

VOICE_PROMPT = """
语音约定：看当前提问和同一人的这几轮对话，判断对方是不是想听你出声。
想听你念、用语音安慰、用某种口音说，或接着上一条语音说「再来一句」时，在一句顺口的回复前加【语音】。只写要念的内容，不附拼音、动作或说明。
对方给了要念的原话，就照原话念，不删改或添词。「语音说出来」「用语音再说一遍」没给新内容时，照念刚才回给这个人的话；「再来一句」才换一句新的。
朗读给定台词按转述处理，沿用熟人群的玩笑尺度：轻度互损、粗口、谐音梗和略低俗的玩笑可以照念，不因带人名或谐音像脏话就拒读，不主动把普通词扩展成色情、诅咒或真实攻击。先看本轮实际要念的内容，历史里拒读过别的句子也不延续成一律禁念的规则；可以念时直接给【语音】和原话，不加道德评价或拒读说明。
明确要求打字或别发语音时加【文字】。单独的「说说」「说一句」、讨论语音功能、引用语音问内容，都照常输出正文，不加标记。历史里别人的请求不算这轮的要求。
自己生成的文字正文带小表情或 emoji 时加【文字】，保留文字里的表情；生成语音正文时不主动添加表情，指定原话仍照原话处理。
标记只供程序使用，是上面正文格式的唯一例外。"""

FIXED_VOICE_PROMPT = """
当前发言者已开启固定语音回复，本轮正文会用语音念出。只写要念的内容，适合直接听，
不添加表情、拼音、动作说明或 Markdown 排版。需要详细回答时照常完整回答，程序会分段朗读。
固定语音持续到管理员用 /语音回复 关闭；本轮即使要求文字也继续用语音。"""


def voice_parts(reply):
    """本地合成管道每次接收一行，长回答在标点附近按 80 字分段。"""
    remaining = " ".join(reply.split())
    parts = []
    while len(remaining) > _MAX_REQUEST_CHARS:
        end = max(remaining.rfind(mark, 0, _MAX_REQUEST_CHARS) for mark in "。！？!?；;，,、 ")
        end = end + 1 if end >= 0 else _MAX_REQUEST_CHARS
        parts.append(remaining[:end].strip())
        remaining = remaining[end:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def voice_reply_format(reply):
    if reply.startswith(("【语音】", "[语音]")):
        return reply[4:].strip(), True
    if reply.startswith(("【文字】", "[文字]")):
        return reply[4:].strip(), False
    return reply, None


class VoiceReply:
    def __init__(self):
        self._next = {}
        self._voices = ()
        self._local = None
        self._stopped = False
        self._synthesis_lock = Lock()

    def start(self, logger):
        self._stopped = False
        self._voices = LocalVoice.available_voices()
        if self._voices:
            try:
                self._local = LocalVoice(DEFAULT_VOICE if DEFAULT_VOICE in self._voices else self._voices[0])
            except OSError as error:
                logger.warning("本地音色未启动：%s", type(error).__name__)

    def close(self):
        self._stopped = True
        self._close_local()

    def _close_local(self):
        if self._local is not None:
            self._local.close()
            self._local = None

    def current_voice(self, room, ctx):
        return room_voice(ctx.room_state, room)

    def fixed(self, message, room_state):
        state = room_state.get(message.chat_id, REPLY_FEATURE) if room_state is not None else None
        return bool(state and state.get(message.sender_id))

    def reply_command(self, message, ctx, args):
        room = message.chat_id
        state = ctx.room_state.get(room, REPLY_FEATURE) or {}
        if not args:
            names = [ctx.members.display_name(wxid, room) or wxid for wxid in state]
            ctx.reply(message, "本群固定语音：" + ("、".join(names) or "暂无") + "。\n" + REPLY_USAGE)
            return
        if not ctx.members.is_admin(message.sender_id):
            ctx.reply(message, "只有管理员能设置固定语音，发 /语音回复 可以查看。")
            return
        parts = args.rsplit(maxsplit=1)
        enabled = parts[-1] != "关闭"
        target = parts[0] if len(parts) == 2 and parts[-1] in ("开启", "关闭") else args
        try:
            wxid = ctx.members.resolve_member_target(room, target, at_members=message.data.get("at_members", ()))
        except MemberError as error:
            ctx.reply(message, str(error) + "\n" + REPLY_USAGE)
            return
        if wxid == ctx.members.account:
            ctx.reply(message, "请指定一位群友。")
            return
        if enabled:
            state[wxid] = True
        else:
            state.pop(wxid, None)
        if state:
            ctx.room_state.put(room, REPLY_FEATURE, state)
        else:
            ctx.room_state.clear(room, REPLY_FEATURE)
        name = ctx.members.display_name(wxid, room) or wxid
        ctx.reply(message, f"本群对{name}的固定语音已{'开启' if enabled else '关闭'}。")

    def command(self, message, ctx, choice):
        room = message.chat_id
        if not choice:
            available = "、".join(self._voices) or "未安装角色音色"
            voice = self.current_voice(room, ctx)
            ctx.reply(message, f"当前音色：{voice}，自称{voice_self_name(voice)}。可选：{available}。")
        elif not ctx.members.is_admin(message.sender_id):
            ctx.reply(message, "只有管理员能改音色，发 /音色 可以查看。")
        elif choice not in self._voices:
            ctx.reply(message, f"当前可用音色：{'、'.join(self._voices) or '无'}。用法：/音色 名称。")
        else:
            ctx.room_state.put(room, VOICE_FEATURE, {"voice": choice})
            ctx.reply(message, f"本群音色已切换为{choice}，现在自称{voice_self_name(choice)}。")

    def reserve(self, message, reply, preference, *, automatic):
        """只选择本轮发送形式并记下冷却时间；不等待合成。"""
        limit = _MAX_REQUEST_CHARS if preference else _MAX_AUTO_CHARS
        if preference is False or not reply or len(reply) > limit or "\n" in reply:
            return False
        room = message.chat_id
        now = monotonic()
        if preference is None and (
                not automatic or now < self._next.get(room, 0) or random() >= _AUTO_CHANCE):
            return False
        self._next[room] = now + _INTERVAL_SECONDS
        return True

    def deliver(self, message, ctx, reply, *, voice, guard, split=False, dispatch=None, on_sent=None):
        """交给慢队列逐段发送；合成失败返回未发送正文，完成或回执不明返回 None。"""
        yield
        parts = voice_parts(reply) if split else [reply]
        for index, part in enumerate(parts):
            submitted = self.send(message, ctx, part, voice=voice, guard=guard, dispatch=dispatch)
            if submitted is None:
                return " ".join(parts[index:])
            if not submitted:
                return None
            if on_sent is not None:
                on_sent(part)
        return None

    def send(self, message, ctx, reply, *, voice, guard, dispatch=None):
        """合成失败返回 None；已提交则返回微信发送结果。"""
        with self._synthesis_lock:
            if self._stopped:
                return None
            now = monotonic()
            if self._voices:
                if voice not in self._voices:
                    return None
                try:
                    if self._local is None:
                        ctx.logger.info("本地音色进程重新启动：%s", voice)
                        self._local = LocalVoice(voice)
                    wav = self._local.synthesize(voice, reply)
                except (OSError, ValueError) as error:
                    ctx.logger.warning("%s语音合成失败，改发文字：%s（%s）", voice, type(error).__name__, error)
                    self._close_local()
                    return None
                if wav is None:
                    ctx.logger.info("%s语音重试后仍超长，改发文字", voice)
                    return None
            else:
                if voice != DEFAULT_VOICE:
                    return None
                try:
                    wav = synthesize_voice(reply)
                except (OSError, SubprocessError, ValueError) as error:
                    ctx.logger.warning("本机语音合成失败，改发文字：%s", type(error).__name__)
                    return None
                voice = "Windows"
        submit = lambda: ctx.reply_voice(message, wav, transcript=reply, delivery_guard=guard)
        sent = dispatch(submit) if dispatch is not None else submit()
        ctx.logger.info("群聊语音回复：%s，音色=%s，合成到回执 %.2f 秒",
                        "已提交" if sent else "未确认", voice, monotonic() - now)
        return sent
