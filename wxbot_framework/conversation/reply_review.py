"""普通回复的依据检查：同一次 Jev 调用区分修订与必要的公开查证。"""
from dataclasses import dataclass
import re

from ..integrations.jev import JevError
from ..messaging.mentions import strip_at_labels
from .tool_router import ACCEPTED, REJECTED, UNAVAILABLE


UNSUPPORTED_LOCAL = {
    "instructions": "候选回复是否把资料无法对应的具体发言、动作或经历，说成某个群友已经实际说过或做过？"
                    "检查群内归因、时间与是否使用已有的人物材料回答当前问题。逐一对照原文的说话者和内容。",
    "true": "候选声称某人已经发过某句话、某种符号或做过具体动作，但原文没有对应记录或说话者不符。"
            "尤其‘刚才已经’之类说法必须有对应的近期群友原文，不能凭旧记忆、性格和机器人之前的话推断；"
            "即使整句在开玩笑，插入的已发生事件仍需有依据。不同的原话不能替换成另一个具体说法或数量。"
            "当前请求需要归纳人物偏好或行为，资料中已有直接相关原话，回复却只说不了解或泛泛接梗，也为true。",
    "false": "正常调侃、反问、主观看法、对当前互动的概括、稳定常识和澄清不算无依据事实。"
             "夸张频次、文学比喻、假想婚姻或未来情景，以及‘你又挖坑让我得罪人’等互动评价，无需逐字证据。"
             "不把玩笑里的‘全群就你’之类夸张当成真实统计。真实断言已由对应人物档案、群友原文、"
             "保留时间和类别的历史记忆或工具资料支持时，也为 false；允许概括原意。"
             "正常的有依据归纳、对其要求的概括和主观看法为false，不要求逐字重复原话。",
}
UNSUPPORTED_PUBLIC = {
    "instructions": "候选是否新增了缺少依据的具体公开事实，或替其他读者、观众概括未经提供的实际反应？"
                    "检查公众反应、特定作品内容、机制、评分数字、当前状况与个体病因，不检查群内玩笑。",
    "true": "提供的资料没有对应依据，候选却肯定某个具体剧情、评分、现价或群外公众反应。"
            "特定游戏机制、个体病因或治疗结论也需对应依据，不能从一条群聊描述补出诊断。"
            "把提问者一个人的好恶升级为很多人同意、读者弃书或观众被劝退，属于新增公众反应；"
            "即使使用口语、夸张和调侃来表达，也需要依据。机器人旧回复和群友个人观点不能证实公众反应。",
    "false": "仅表达自己的偏好、观感、反问，或概括对方刚刚表达的好恶；没有替其他读者和观众发言，"
             "也没添加具体剧情、数据或现状。稳定常识无需引用。已有资料能支持的事实、"
             "已注明是某位群友的观点，或明确按用户给出的条件讨论且未额外证实或添加细节时也为 false。"
             "群友之间的假想和夸张玩笑不属于公开事实。",
}
NEEDS_SEARCH = {
    "instructions": "判断为回答当前问题，是否有必要补查尚缺依据的公开事实。结合候选回复识别缺口，"
                    "单独判断补查的必要性，不因上一项判断的结果猜测。",
    "true": "当前问题确实依赖尚缺资料的具体公开事实，如询问某段剧情发生了什么、公众评分和口碑依据、"
            "当前价格或地区政策。需要获取对应资料才能回答其核心问题。",
    "false": "群友身份、经历、刚才谁说了什么或群梗，只能回到群内资料纠正或澄清。"
             "只问主观看法、感受、开玩笑或稳定常识时无需搜索；候选擅自附加的剧情、"
             "‘很多人觉得’或‘劝退不少人’等旁支说法可以删去，不值得为这些多余断言触发搜索。"
             "提供的工具资料已经支持本问时也无需补查。",
}
VOICE_CAPABILITY = "语音功能只会把文字朗读成语音，不能实际唱歌。"
UNSUPPORTED_SELF = {
    "instructions": "核对机器人对自己先前的话、动机、工具操作或语音动作的陈述，使用‘本轮确认事实’中的证据。",
    "true": "拒绝承认已找到的匹配原话、把没找到说成从未说过、指责群友编造、用旧说法冒充事实、"
            "捏造自己想捧场等动机或查过、查不了、已发送等动作。也包括声称唱过、跑调或承诺稍后唱歌，"
            "把未确认或失败说成已完成，或在应先解释只能朗读时直接开始念内容。",
    "false": "按原话和当时语境承认或更正，待核实的地方坦白不清楚。"
             "如实表达能力限制、已确认的朗读或回执状态。群友接受后直接提供要朗读的内容也算。",
}
UNSUPPORTED_EXPLANATION = {
    "instructions": "只判断这一件事：候选是否给机器人先前的说法或失误编了程序未确认的具体理由或动机？",
    "true": "用‘当时就是想给你捧场’、‘看你心情不佳所以故意哄你’、‘我就是逗你才那样说’、"
            "‘我纯是为了蹭饭才说的’、‘我查不到所以才这么答’等解释自己的旧回答，而程序证据没有给出这个原因。"
            "原话里玩笑提到一件好处，不等于程序证实当时真实动机。即使是群聊调侃也判真。",
    "false": "承认‘我那句说得太满了’、按原话修正判断，或坦白不知道原因。"
             "指出原话出自确有记录的假设、玩笑或问答场景也可。",
}
CORRECTION_EVADED = {
    "instructions": "只判断候选有没有正面回应对机器人上一句或历史原话的纠正。对照‘本轮确认事实’里的原话核对。",
    "true": "找到能对应的原话，却没有明确承认那句是自己说的。原断言被质疑且依据不足，"
            "只换一个说法、谈泛泛的可能性或反问群友，却没有收回或说明先前说得太确定。"
            "没有可信原话却回避说明查不到。追问实际动作时，明明有程序记录却转移话题。",
    "false": "明确承认自己说过对应的话；需要更正时承认前面哪点说错或说得太满，按证据更正，"
             "当前无可靠事实就说明依据缺口。已有程序确认的动作则直接回答。可以口语化，无需使用固定句式。",
}
UNVERIFIED_REPLACEMENT = {
    "instructions": "机器人撤回一条旧说法时，有没有用另一个同样缺少证据的具体判断来替换它？只看新判断。",
    "true": "旧答说‘高手’，候选又说‘前十万哪算得上高手’或‘就是及格线’，但资料没有总人数、榜单分布或分段标准。"
            "也包括纠错后换成其他没有证据的结论，即使表述简短或像调侃。",
    "false": "说明原判断太确定，并承认现有资料不足；只讨论明确的假设或按给出的榜单事实作有限判断。"
             "确认自己说过哪句话、复述原话或描述能确定的前提，也不属于无依据的新判断。",
}


def _speech_terms(text):
    terms = set()
    for word in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if word.isascii():
            terms.add(word)
        else:
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
    return terms


def correction_evidence(jev, question, rows, *, quote=None, quote_id=""):
    """按明示引用和关键词取候选，语义上是否对应由 Jev 判断。"""
    quoted = " ".join(strip_at_labels(quote["text"]).split()) if quote else ""
    direct = next((row for row in rows if quote_id and row["context_message_id"] == quote_id), None)
    if direct is None and quoted:
        direct = next((row for row in rows if " ".join(strip_at_labels(row["text"]).split()) == quoted), None)
    if direct is not None:
        candidates = [direct]
    else:
        terms = _speech_terms(question + " " + quoted)
        candidates = sorted(rows, key=lambda row: len(terms & _speech_terms(row["text"])), reverse=True)[:10]
    evidence = [{"编号": row["id"], "时间": row["received_at"], "形式": "朗读语音" if row["context_kind"] == "voice" else "文字",
                 "原话": strip_at_labels(row["text"])} for row in candidates]
    if direct is not None or not evidence:
        return evidence
    checks = [{"instructions": f"编号 {row['编号']} 的机器人历史原话是否直接对应群友现在核对的那句话或语音？",
               "true": "是同一说法、同一件事的相反说法，或直接对应其质疑的语音。保留玩笑等条件。",
               "false": "只是同名、同主题、一般闲聊、无关状态，或无法确定指的是这条。"} for row in evidence]
    try:
        scores = jev.noul({"现在的纠正或追问": question, "引用线索": quoted, "候选原话": evidence}, checks, timeout=2)
        ranked = sorted(zip(scores, evidence, strict=True), key=lambda pair: pair[0], reverse=True)
    except (JevError, OSError, ValueError):
        return []
    return [row for score, row in ranked[:3] if score >= 0.7]


@dataclass(frozen=True)
class ReplyReview:
    status: str
    local: float | None = None
    public: float | None = None
    search: float | None = None
    error: str | None = None
    own: float | None = None

    @property
    def needs_search(self):
        return self.search is not None and self.search >= 0.7


def reply_evidence(snapshot, context, *, quote=None, quote_is_bot=False, tools=(), room_game=None,
                   turn_context="", self_facts=None):
    rows = {}
    for row in (*snapshot.messages, *snapshot.group_messages, *snapshot.history):
        key = row.get("id", (row.get("speaker_name"), row["text"]))
        rows[key] = {
            "时间": str(row.get("received_at", "")),
            "角色": "机器人" if row.get("bot_generated") or row.get("is_self") else "群友",
            "发言者": row.get("speaker_name", snapshot.speaker_name),
            "编号": row.get("speaker", snapshot.speaker_tag),
            "类型": row.get("context_kind", ""), "原文": row["text"],
        }
    evidence = {
        "当前发言者": {"编号": snapshot.speaker_tag, "名称": snapshot.speaker_name},
        "已确认成员称呼": snapshot.participants,
        "提问者档案": context.speaker_profile, "相关人物档案": context.people,
        "相关成员原话": context.person_messages,
        "聊天原文": sorted(rows.values(), key=lambda row: row["时间"]),
        "历史记忆": [{key: item[key] for key in ("name", "kind", "content", "updated_at") if key in item}
                 for item in context.knowledge],
        "本次引用": {"角色": "机器人" if quote_is_bot else "群友", **quote} if quote else None,
        "本轮接话": turn_context, "工具资料": list(tools), "接龙进度": room_game,
    }
    if self_facts:
        evidence["本轮确认事实"] = self_facts
    return evidence


def review_reply(jev, question, answer, evidence):
    if jev is None:
        return ReplyReview(UNAVAILABLE, error="not-configured")
    checks = [UNSUPPORTED_LOCAL, UNSUPPORTED_PUBLIC, NEEDS_SEARCH]
    facts = evidence.get("本轮确认事实") or {}
    if facts:
        checks.append(UNSUPPORTED_SELF)
    if "原话核对" in facts:
        checks.extend((UNSUPPORTED_EXPLANATION, CORRECTION_EVADED, UNVERIFIED_REPLACEMENT))
    try:
        local, public, search, *own = jev.noul({"提问": question, "拟发送的回答": answer, "依据": evidence}, checks, timeout=2)
    except Exception as error:
        return ReplyReview(UNAVAILABLE, error=type(error).__name__)
    return ReplyReview(REJECTED if max(local, public, *own) >= 0.7 else ACCEPTED,
                       local, public, search, own=max(own) if own else None)
