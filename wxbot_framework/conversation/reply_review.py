"""普通回复的依据检查：同一次 Jev 调用区分修订与必要的公开查证。"""
from dataclasses import dataclass

from .tool_router import ACCEPTED, REJECTED, UNAVAILABLE


UNSUPPORTED_LOCAL = {
    "instructions": "候选回复是否把资料无法对应的具体发言、动作或经历，说成某个群友已经实际说过或做过？"
                    "只检查群内归因及时间，不检查公开作品知识或主观好恶。逐一对照原文的说话者和内容。",
    "true": "候选声称某人已经发过某句话、某种符号或做过具体动作，但原文没有对应记录或说话者不符。"
            "尤其‘刚才已经’之类说法必须有对应的近期群友原文，不能凭旧记忆、性格和机器人之前的话推断；"
            "即使整句在开玩笑，插入的已发生事件仍需有依据。不同的原话不能替换成另一个具体说法或数量。",
    "false": "正常调侃、反问、主观看法、对当前互动的概括、稳定常识和澄清不算无依据事实。"
             "夸张频次、文学比喻、假想婚姻或未来情景，以及‘你又挖坑让我得罪人’等互动评价，无需逐字证据。"
             "不把玩笑里的‘全群就你’之类夸张当成真实统计。真实断言已由对应人物档案、群友原文、"
             "保留时间和类别的历史记忆或工具资料支持时，也为 false；允许概括原意。"
             "没有陈述群友已经实际说过或做过某件具体事情时也为 false。",
}
UNSUPPORTED_PUBLIC = {
    "instructions": "候选是否新增了缺少依据的具体公开事实，或替其他读者、观众概括未经提供的实际反应？"
                    "只检查候选中的公众反应、特定作品内容、评分数字及当前状况，不检查群内玩笑和发言归因。",
    "true": "提供的资料没有对应依据，候选却肯定某个具体剧情、评分、现价或群外公众反应。"
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


@dataclass(frozen=True)
class ReplyReview:
    status: str
    local: float | None = None
    public: float | None = None
    search: float | None = None
    error: str | None = None

    @property
    def needs_search(self):
        return self.public is not None and self.public >= 0.7 and self.search >= 0.7


def reply_evidence(snapshot, context, *, quote=None, quote_is_bot=False, tools=(), room_game=None,
                   turn_context=""):
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
    return {
        "当前发言者": {"编号": snapshot.speaker_tag, "名称": snapshot.speaker_name},
        "已确认成员称呼": snapshot.participants,
        "提问者档案": context.speaker_profile, "相关人物档案": context.people,
        "聊天原文": sorted(rows.values(), key=lambda row: row["时间"]),
        "历史记忆": [{key: item[key] for key in ("name", "kind", "content", "updated_at") if key in item}
                 for item in context.knowledge],
        "本次引用": {"角色": "机器人" if quote_is_bot else "群友", **quote} if quote else None,
        "本轮接话": turn_context, "工具资料": list(tools), "接龙进度": room_game,
    }


def review_reply(jev, question, answer, evidence):
    if jev is None:
        return ReplyReview(UNAVAILABLE, error="not-configured")
    try:
        local, public, search = jev.noul({"提问": question, "拟发送的回答": answer, "依据": evidence},
                                        [UNSUPPORTED_LOCAL, UNSUPPORTED_PUBLIC, NEEDS_SEARCH], timeout=2)
    except Exception as error:
        return ReplyReview(UNAVAILABLE, error=type(error).__name__)
    return ReplyReview(REJECTED if max(local, public) >= 0.7 else ACCEPTED, local, public, search)
