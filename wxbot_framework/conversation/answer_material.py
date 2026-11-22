"""按当前话题收集群聊证据，并为会变动的公开事实提供查证路径。"""
from dataclasses import dataclass

from .context_tools import ContextToolError
from .evidence_tools import ConversationEvidence, tool
from .tool_loop import run_tools
from ..integrations.jev import JevError
from ..integrations.llm import LLMContentBlocked, LLMError, LLMFormatError
from ..timeutil import format_local


ROUTING = (
    {"instructions": "本轮要答的核心是否依赖近期可能变化的公开状态，需要重新查证？",
     "true": "核实某公众人物现在的任职、谁已离任或复职、某版本现状、当日结果、消息是否已证实、某作品当前的评分。"
             "包括短回复在纠正机器人的旧说法，或当前问题把新消息当作关键前提。",
     "false": "只是翻译、复述、创作、讲稳定常识、普通闲聊，或问群友和私人话题。只判断本轮想弄清的事。"},
    {"instructions": "本轮是否需要主动找本群原文或档案，才能辨清对象、解释所指内容或回答？",
     "true": "不知道指哪条旧消息、承诺、决定或人，当前材料不足，需要按话题、人物或原话搜索。"
             "要核对自己的历史说法或某位群友此前说的细节。",
     "false": "本次明确引用和已给前文已经足够，或只是接梗、寒暄、问一般知识。"},
    {"instructions": "如果机器人继续回这句，判断它是否在反驳上文关于真实公开主体的近期状态。无需对方提出疑问句。不要判断谁说得对。",
     "true": "正在反驳公众人物离任、复职、近期调动、媒体最新披露或类似近期可查事实。即便本句很短，也可由前文确定。",
     "false": "前文与本轮不同话题、口语玩笑、群友退群或退出游戏、纯翻译，或者不存在公开说法的纠正。"},
)

SYSTEM = """为群聊回答按需找资料，结合当前问题、同一话题的前文和引用理解目标。
明确引用被要求翻译、解释或整理时，处理那条引用。近期消息只帮助定位，别接到同时进行的另一件事。
先判断缺的是原话、谁说的、当时的决定、档案，还是当前公开事实。资料够用时不重复查询。
群友消息和机器人已发的回复是发言记录，人物记忆需要区分本人陈述、他人提及、群梗及人工确认。
需要核对新的公开状态时，调用 search_public。查询只写公开主体和待查事实，优先查官方公告及权威近期来源。
看清来源时间、事件发生时间和当前状态。结果陈旧、只谈旧职务或讲的是别的人时，换中立关键词补查一次。
整理完成只返回 {"依据":["实际来源编号"]}；选择足以回答本轮的问题的少量资料。没有可用依据时返回空数组。"""

PUBLIC_SUPPORT = {"instructions": "本轮检索资料是否直接、及时、可靠地支持拟回答中的现状或变更事实？",
                  "true": "有明确出处、时间和正文，能直接证实这一主体在本轮所问时间的状态或变化。答案忠于原意。",
                  "false": "只有过期简介、标题猜测、另一主体、答非所问、群友原话、机器人旧回复、传言，或没有能支持断言的资料。"
                           "只有搜索成功的状态也算 false。"}


def _recent(snapshot):
    return [{"发言人": row.get("speaker_name", "群友"), "内容": row["text"][:400]} for row in snapshot.history[-10:]]


def material_needs(judge, question, snapshot, quote, previous):
    if judge is None:
        return False, False
    state = {"这轮要回答的问题": question, "明确引用": quote, "本群前文": _recent(snapshot),
             "同一人的上一轮": {"问题": previous.question, "已发回答": previous.text} if previous else None}
    try:
        scores = judge.noul(state, ROUTING, timeout=2)
    except (JevError, OSError, ValueError):
        return False, False
    if len(scores) != len(ROUTING):
        return False, False
    return scores[0] >= 0.55 or scores[2] >= 0.52, scores[1] >= 0.65


@dataclass(frozen=True)
class AnswerMaterial:
    local: tuple
    public: tuple

    @property
    def public_text(self):
        return "\n\n".join(row["内容"] for row in self.public)


def gather_answer_material(model, members, knowledge, message, snapshot, question, quote, *, current, search, guard, logger):
    evidence = ConversationEvidence(members, knowledge, snapshot, message)
    task = {"当前问题": question, "明确引用": quote, "当前时间": format_local(members.now()),
            "需要核对近期公开情况": current, "近期收发对话": evidence.messages(snapshot.history[-12:])}
    if search is not None:
        def find_public(query):
            if not isinstance(query, str) or not 2 <= len(query.strip()) <= 260:
                raise ContextToolError()
            result = search(f"{query.strip()}\n核对时间：{task['当前时间']}。优先使用近期一手来源，注明出处、发布日期和关键事实。")
            if not result.ok:
                return {"状态": "公开查询失败", "原因": result.note}
            return {"状态": "收到搜索资料", "资料": evidence.record("公开搜索结果", result.text[:5200])}
        evidence.tools.register(tool("search_public", "查询本轮涉及的近期公开事实，返回含出处的公开资料。",
                                     {"query": {"type": "string", "description": "公开主体和待核实的变更，不含群友私话"}},
                                     find_public, required=("query",)))
    initial = set(evidence.records)
    try:
        output = run_tools(model, SYSTEM, task, evidence.tools, guard=guard, logger=logger,
                           max_tokens=None, rounds=2 if current else 1, repair=False)
        ids = output.get("依据")
        if not isinstance(ids, list):
            raise LLMFormatError("群聊取材没有给出来源编号")
        ids = [key for key in ids if isinstance(key, str) and key in evidence.records][:12]
    except LLMContentBlocked:
        raise
    except (LLMError, TimeoutError) as error:
        logger.info("群聊取材提前结束：%s", type(error).__name__)
        ids = [key for key in evidence.records if key not in initial]
        if not ids:
            return None
    records = [evidence.records[key] for key in dict.fromkeys(ids)]
    local = tuple(row for row in records if row["类别"] != "公开搜索结果")
    public = tuple(row for row in records if row["类别"] == "公开搜索结果")
    logger.info("群聊取材定稿：锚点=%s 原话=%s 公开来源组=%d 时效=%s", snapshot.anchor_id,
                [row.get("消息编号") or row.get("条目编号") for row in local], len(public), current)
    return AnswerMaterial(local, public)


def supports_current_answer(judge, question, reply, sources, *, now):
    if judge is None or not sources:
        return False
    try:
        scores = judge.noul({"问题": question, "当前时间": format_local(now), "拟回答": reply,
                             "本轮公开资料": sources[:10500]}, [PUBLIC_SUPPORT], timeout=3)
    except (JevError, OSError, ValueError):
        return False
    return len(scores) == 1 and scores[0] >= 0.75
