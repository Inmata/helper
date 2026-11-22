"""把本群的原话和记忆封装为当前请求可以使用的只读资料。"""
from datetime import timedelta
from difflib import SequenceMatcher

from .context_tools import ContextTool, ContextToolError, ContextTools
from .members import MemberError
from ..timeutil import parse_utc


def tool(name, description, properties, action, *, required=()):
    return ContextTool(name, description, {"type": "object", "properties": properties,
                                           "required": list(required), "additionalProperties": False}, action)


class ConversationEvidence:
    def __init__(self, members, knowledge, snapshot, message):
        self.members = members
        self.knowledge = knowledge
        self.room = snapshot.room
        self.snapshot = snapshot
        self.message = message
        self.now = members.now()
        self.records = {}
        self._message_ids = {}
        self._people = None
        self._candidates = set()
        self._identified = set()
        self.tools = ContextTools([
            tool("search_messages", "查本群原话。可按关键词、时间、群友或机器人筛选。命中后可用 read_context 核对前后文。",
                 {"keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                  "speaker": {"type": "string", "description": "any、member、bot 或 find_people 返回的人物编号"},
                  "days": {"type": "integer", "minimum": 1, "maximum": 14}}, self.search_messages),
            tool("read_context", "读某个已找到的消息和它在本群的邻近消息，用于辨别指代、话题和后来更正。",
                 {"source": {"type": "string", "description": "当前资料中的消息来源编号"}},
                 self.read_context, required=("source",)),
            tool("find_people", "按名字、外号、艾特或请求人定位已知群友。近似匹配供澄清，不能直接当作已确认身份。",
                 {"name": {"type": "string", "description": "名字、别名、请求人或本次艾特"}},
                 self.find_people, required=("name",)),
            tool("read_memory", "读取选定群友的档案，或按关键词找本群公共记忆。返回类别、版本、来源和内容。",
                 {"person": {"type": "string", "description": "find_people 返回的已明确人物编号，省略时查群记忆"},
                  "topic": {"type": "string"}}, self.read_memory),
        ])

    def record(self, kind, text, **details):
        key = f"E{len(self.records) + 1}"
        entry = {"来源编号": key, "类别": kind, "内容": text, **details}
        self.records[key] = entry
        return entry

    def messages(self, rows):
        result = []
        for row in rows:
            mid = row["id"]
            if mid not in self._message_ids:
                entry = self.record("机器人已发原话" if row.get("bot_generated") else "群友原话",
                                    row["text"][:800], 发言人=row.get("speaker_name", "群友"),
                                    时间=row["received_at"], 消息编号=mid)
                if row.get("reply_to"):
                    entry["回复目标消息编号"] = row["reply_to"]
                self._message_ids[mid] = entry
            result.append(self._message_ids[mid])
        return result

    @property
    def people(self):
        if self._people is None:
            self._people = {f"P{n}": p for n, p in enumerate(self.knowledge.conversation_people(self.room), 1)}
        return self._people

    def find_people(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ContextToolError()
        name = name.strip().casefold()
        matches, similar = [], []
        for key, person in self.people.items():
            labels = (person["name"], *person["aliases"])
            candidate = {"人物编号": key, "名字": person["name"], "别名": person["aliases"],
                         "有档案": type(person["id"]) is int}
            if (name in {label.casefold() for label in labels}
                    or name == "请求人" and person["wxid"] == self.message.sender_id
                    or name == "本次艾特" and person["wxid"] in self.message.data.get("at_members", ())):
                matches.append(candidate)
            elif max(SequenceMatcher(None, name, label.casefold()).ratio() for label in labels) >= 0.65:
                similar.append(candidate)
        candidates = (matches or similar)[:8]
        self._candidates.update(person["人物编号"] for person in candidates)
        if len(matches) == 1:
            self._identified.add(matches[0]["人物编号"])
        return {"状态": "唯一" if len(matches) == 1 else "需要消歧" if candidates else "未找到",
                "候选": candidates}

    def search_messages(self, keywords=None, speaker="any", days=14):
        if keywords is None:
            keywords = []
        if (not isinstance(keywords, list) or len(keywords) > 8
                or any(not isinstance(word, str) or not 1 <= len(word.strip()) <= 80 for word in keywords)
                or type(days) is not int or not 1 <= days <= 14 or not isinstance(speaker, str)
                or speaker not in ("any", "bot", "member") and speaker not in self._candidates):
            raise ContextToolError()
        words = tuple(word.strip().casefold() for word in keywords)
        since = self.now - timedelta(days=days)
        if speaker == "bot":
            rows = [{"bot_generated": True, "speaker_name": "机器人", **row}
                    for row in self.members.own_replies(self.snapshot) if parse_utc(row["received_at"]) >= since]
        else:
            person = self.people.get(speaker) if speaker not in ("any", "member") else None
            rows = self.members.recent_rows(self.room, before_id=self.snapshot.anchor_id, since=since,
                                            sender_id=person["wxid"] if person else None, include_bot=speaker == "any",
                                            context=True, limit=90, keywords=words)
        ranked = []
        for row in rows:
            matches = sum(word in row["text"].casefold() for word in words)
            if matches or not words:
                ranked.append((matches, row["id"], row))
        ranked.sort(reverse=True)
        rows = [row for _, _, row in sorted(ranked[:12], key=lambda item: item[1])]
        found = self.messages(rows if speaker == "bot" else self.members.label_speakers(rows, self.room))
        return {"状态": "找到" if found else "未找到", "消息": found}

    def read_context(self, source):
        if not isinstance(source, str) or "消息编号" not in self.records.get(source, {}):
            raise ContextToolError()
        mid = self.records[source]["消息编号"]
        since = self.now - timedelta(days=14)
        before = self.members.recent_rows(self.room, before_id=mid, since=since, context=True, include_bot=True, limit=8)
        after = self.members.recent_rows(self.room, before_id=self.snapshot.anchor_id, after_id=mid, since=since,
                                         context=True, include_bot=True, earliest=True, limit=12)
        entry = self.records[source]
        reply_to = entry.get("回复目标消息编号")
        result = {"状态": "找到", "焦点": entry,
                  "前后文": self.messages(self.members.label_speakers([*before, *after], self.room))}
        if reply_to:
            target = self.members.recent_rows(self.room, before_id=reply_to + 1, after_id=reply_to - 1,
                                             since=since, context=True, include_commands=True, limit=1)
            if target:
                result["实际回复对象"] = self.messages(self.members.label_speakers(target, self.room))[0]
        return result

    def read_memory(self, person="", topic=""):
        if (not isinstance(person, str) or not isinstance(topic, str) or len(topic) > 180
                or person and person not in self._identified or not (person or topic.strip())):
            raise ContextToolError()
        if person:
            target = self.people[person]
            if type(target["id"]) is not int:
                return {"状态": "未找到档案", "资料": []}
            try:
                entries = self.knowledge.profile_material(self.room, target["id"])["memories"]
            except MemberError:
                entries = []
            if topic.strip():
                words = topic.casefold().split()
                entries.sort(key=lambda row: sum(word in (row["slot"] + row["content"]).casefold() for word in words),
                             reverse=True)
        else:
            entries = [row for row in self.knowledge.search(self.room, topic, limit=16) if row["scope"] == "group"]
        records = [self.record(row["kind"], row["content"][:500], 条目=row["slot"], 更新时间=row["updated_at"],
                               人物=self.people[person]["name"] if person else "本群",
                               条目编号=row["id"], 修订=row["revision"],
                               原始出处=row.get("sources") or row.get("last_source_at"))
                   for row in entries[:16]]
        return {"状态": "找到" if records else "未找到", "资料": records}
