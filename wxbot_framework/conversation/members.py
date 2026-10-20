"""成员身份、别名与群目录，按账号、群及成员 ID 隔离。

聊天原文与快照由 ``conversation.history`` 负责；本模块保留公开门面，
把历史相关调用转发给历史实现，插件与旧代码无需改写。
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..account import validate_account
from ..config import validate_name
from ..messaging.text import validate_mentions
from ..storage.database import Database
from ..storage.migrations import initialize_base_schema
from ..timeutil import iso_utc, parse_utc


class MemberError(ValueError):
    pass


class SnapshotExpired(RuntimeError):
    """清空或跨天后，取消仍在生成/等待发送的总结。"""


BEIJING = timezone(timedelta(hours=8))
INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u3164\u115f\u1160\uffa0\u2800\u00a0\u2005"
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
})


@dataclass(frozen=True)
class ChatSnapshot:
    member: Member
    day: str
    generation: int
    messages: tuple
    scope: str = "today"
    anchor_id: int = 0

    @property
    def room(self):
        return self.member.room


@dataclass(frozen=True)
class GroupChatSnapshot:
    room: str
    day: str
    generation: int
    messages: tuple
    scope: str = "today"
    anchor_id: int = 0


@dataclass(frozen=True)
class ConversationSnapshot:
    room: str
    day: str
    generation: int
    speaker_name: str
    messages: tuple
    current: str
    group_messages: tuple = ()
    anchor_id: int = 0
    history: tuple = ()


@dataclass(frozen=True)
class Member:
    room: str
    name: str
    display_name: str
    wxid: str | None
    aliases: tuple[str, ...]
    self_named: bool = False


def name_key(value):
    if not isinstance(value, str) or len(value) > 64 or any(c in value for c in "\0\n\r"):
        raise MemberError("成员称呼应为 1–64 字符的单行文本")
    cleaned = "".join(ch for ch in unicodedata.normalize("NFKC", value).strip() if ch not in INVISIBLE_CHARS).strip()
    if not cleaned:
        raise MemberError("成员称呼应为 1–64 字符的单行文本")
    return cleaned.casefold().translate(HOMOGLYPHS)


def parse_json_list(raw):
    """容错解析数据库中的 JSON 字符串列表（兼容空串、NULL 或手动输入的逗号/顿号分隔文本）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = [part.strip() for part in re.split(r"[,，、;\n]+", text) if part.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if isinstance(item, (str, int, float)) and str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def parse_aliases(raw):
    """容错解析并归一化人物别名列表，过滤非法值并去重。"""
    result = []
    seen = set()
    for item in parse_json_list(raw):
        try:
            key = name_key(item)
        except MemberError:
            continue
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


class MemberStore:
    def __init__(self, config_file, account, *, now=None):
        self.account = validate_account(account)
        self.config_file = Path(config_file).resolve()
        options = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        if not isinstance(options, dict) or not isinstance(options.get("groups"), dict):
            raise MemberError("成员配置应包含 groups 对象")
        database = options.get("database_file", ".runtime/member_data.sqlite3")
        if not isinstance(database, str) or not database.strip() or "\0" in database:
            raise MemberError("database_file 应为数据库路径")
        self.path = (self.config_file.parent / database).resolve()
        self.history_enabled = options.get("history_enabled", False)
        if type(self.history_enabled) is not bool:
            raise MemberError("history_enabled 必须是布尔值")
        self.daily_clear = options.get("daily_clear", False)
        if type(self.daily_clear) is not bool:
            raise MemberError("daily_clear 必须是布尔值")
        self._clock = now or (lambda: datetime.now(timezone.utc))
        self.owner_wxid = options.get("owner_wxid")
        if self.owner_wxid is not None:
            validate_account(self.owner_wxid)
            if self.owner_wxid == self.account:
                raise MemberError("维护者必须是独立于机器人本号的微信账号")
        self.groups = set(options["groups"])
        self.profiles = []
        for room, profiles in options["groups"].items():
            validate_mentions(room, [self.account])
            if not isinstance(profiles, list):
                raise MemberError("每个群的成员配置应为列表")
            keys, names, ids = set(), set(), set()
            for profile in profiles:
                if not isinstance(profile, dict):
                    raise MemberError("成员资料应为 JSON 对象")
                key = validate_name(profile.get("key"))
                name = profile.get("name")
                display = profile.get("display_name", name)
                aliases = profile.get("aliases", [])
                if not isinstance(aliases, list):
                    raise MemberError("aliases 应为称呼列表")
                normalized = {name_key(value) for value in [name, display, *aliases]}
                wxid = profile.get("wxid")
                if wxid is not None:
                    validate_mentions(room, [wxid])
                if key in keys or normalized & names or (wxid and wxid in ids):
                    raise MemberError("同一群的成员标识、称呼和微信 ID 不能重复")
                keys.add(key)
                names.update(normalized)
                if wxid:
                    ids.add(wxid)
                self.profiles.append((room, key, name.strip(), display.strip(), wxid, normalized))
        self.database = Database(self.path)
        self._prepared = False
        # 聊天原文拆到独立模块；成员库仍持有同一连接与锁。
        from .history import ConversationHistory
        self.transcript = ConversationHistory(self)

    @classmethod
    def from_config(cls, config, directory):
        filename = config.get("members_config_file")
        if filename is None:
            return None
        if not isinstance(filename, str) or not filename.strip() or "\0" in filename:
            raise MemberError("members_config_file 应为成员配置文件路径或 null")
        if not config.get("wechat_account"):
            raise MemberError("启用成员目录需要固定 wechat_account，以隔离各机器人账号的数据")
        return cls(Path(directory) / filename, config["wechat_account"])

    def connection(self):
        """返回唯一连接；首次使用时建表并导入配置档案。"""
        if not self._prepared:
            self.prepare()
        return self.database.connection()

    @contextmanager
    def transaction(self):
        """共享事务；进入前确保建表完成，写路径不依赖外部先调用 initialize。"""
        self.connection()
        with self.database.transaction() as db:
            yield db

    @property
    def lock(self):
        return self.database.lock

    def now(self):
        """当前时间；测试可注入时钟，生产使用 UTC。"""
        return self._clock()

    def prepare(self):
        """显式初始化：建表并导入配置中的成员档案，可重复调用。"""
        with self.database.lock:
            if self._prepared:
                return
            try:
                db = self.database.connection()
                initialize_base_schema(db)
                self._seed_profiles(db)
            except BaseException:
                self.database.discard()
                raise
            self._prepared = True

    def _seed_profiles(self, db):
        with self.database.transaction():
            for room, key, name, display, wxid, aliases in self.profiles:
                existing = db.execute("SELECT wxid FROM people WHERE account=? AND room=? AND person_key=?",
                                      (self.account, room, key)).fetchone()
                if existing and existing["wxid"] and wxid and existing["wxid"] != wxid:
                    raise MemberError(f"{name} 的配置 ID 与已保存绑定不同，请先核对")
                db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account,room,person_key) DO UPDATE SET
                        name=CASE WHEN people.self_named=0 THEN excluded.name ELSE people.name END,
                        display_name=CASE WHEN people.self_named=0 THEN excluded.display_name ELSE people.display_name END,
                        wxid=COALESCE(people.wxid,excluded.wxid)""", (self.account, room, key, name, display, wxid))
                for alias in aliases:
                    old = db.execute("SELECT person_key FROM aliases WHERE account=? AND room=? AND alias=?",
                                     (self.account, room, alias)).fetchone()
                    if old and old["person_key"] != key:
                        raise MemberError(f"称呼 {alias} 已属于另一位成员")
                    db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))

    def initialize(self):
        self.prepare()
        self.rollover()

    def validate_member_id(self, room, wxid):
        """校验群 ID 与微信 ID 格式，供历史写入复用成员库的既有校验。"""
        validate_mentions(room, [wxid])

    # —— 聊天原文与快照：公开门面，实现在 conversation/history.py ——

    def rollover(self):
        return self.transcript.rollover()

    def clear(self, room):
        return self.transcript.clear(room)

    def today_snapshot(self, name, room):
        return self.transcript.today_snapshot(name, room)

    def today_group_snapshot(self, room):
        return self.transcript.today_group_snapshot(room)

    def snapshot_is_current(self, snapshot):
        return self.transcript.snapshot_is_current(snapshot)

    def conversation_snapshot(self, message, *, group_limit=25):
        return self.transcript.conversation_snapshot(message, group_limit=group_limit)

    def delivery_guard(self, snapshot):
        return self.transcript.delivery_guard(snapshot)

    def record(self, message):
        return self.transcript.record(message)

    def record_bot_reply(self, room, text):
        return self.transcript.record_bot_reply(room, text)

    def message_id(self, message):
        return self.transcript.message_id(message)

    def latest_id(self, room):
        return self.transcript.latest_id(room)

    def recent_rows(self, room, **options):
        return self.transcript.recent_rows(room, **options)

    def label_speakers(self, rows, room):
        return self.transcript.label_speakers(rows, room)

    def recent_snapshot(self, room, **options):
        return self.transcript.recent_snapshot(room, **options)

    def reset_context(self, room):
        return self.transcript.reset_context(room)

    def close(self):
        """关闭共用连接；由装配方在停止流程末尾调用。"""
        self.database.close()

    def history(self, name, room=None, *, limit=100, since=None):
        return self.transcript.history(name, room, limit=limit, since=since)

    def personality(self, room):
        from .personality import PersonalityState
        return PersonalityState(room)

    def set_personality(self, room, name):
        raise MemberError("已固定为熟人损友风格，不再切换人格")

    def personality_is_current(self, state):
        return self.personality(state.room) == state

    @contextmanager
    def personality_delivery_guard(self, state):
        from .personality import PersonalityExpired
        with self.database.lock:
            if not self.personality_is_current(state):
                raise PersonalityExpired("人格已切换")
            yield

    def _member(self, row):
        if row["person_key"] is None:
            return Member(row["room"], row["name"], row["name"], row["wxid"], (), False)
        aliases = self.connection().execute(
            "SELECT alias FROM aliases WHERE account=? AND room=? AND person_key=? ORDER BY alias",
            (self.account, row["room"], row["person_key"])).fetchall()
        return Member(row["room"], row["name"], row["display_name"], row["wxid"], tuple(r[0] for r in aliases),
                      bool(row["self_named"]))

    def remember_wechat_name(self, wxid, name):
        """微信原始名字按账号 ID 缓存，不产生任何群内绑定或别名。"""
        validate_account(wxid)
        normalized = name_key(name)
        if any(ord(ch) < 32 for ch in name):
            raise MemberError("微信名不能包含控制字符")
        cleaned = "".join(ch for ch in name.strip() if ch not in INVISIBLE_CHARS).strip()
        with self.transaction() as db:
            db.execute("""INSERT INTO wechat_names VALUES(?,?,?,?) ON CONFLICT(account,wxid)
                DO UPDATE SET name=excluded.name,name_key=excluded.name_key""",
                (self.account, wxid, cleaned, normalized))

    def wechat_name(self, wxid):
        with self.database.lock:
            row = self.connection().execute("SELECT name FROM wechat_names WHERE account=? AND wxid=?",
                                             (self.account, wxid)).fetchone()
            return row[0] if row else None

    def is_impersonating_name(self, wxid, room, candidate_name):
        """防冒充校验：未绑定该身份的成员，若微信昵称与管理员、已登记成员或知识库人物主名/别名撞名（含同形字），视为撞名。"""
        try:
            key = name_key(candidate_name)
        except MemberError:
            return True
        with self.database.lock:
            db = self.connection()
            if self.owner_wxid and wxid != self.owner_wxid:
                owner_name = db.execute("SELECT name_key FROM wechat_names WHERE account=? AND wxid=?",
                                        (self.account, self.owner_wxid)).fetchone()
                if owner_name and owner_name[0] == key:
                    return True
            if room is not None:
                clash_member = db.execute("""SELECT p.wxid FROM people p JOIN aliases a USING(account,room,person_key)
                    WHERE a.account=? AND a.room=? AND a.alias=?""", (self.account, room, key)).fetchone()
                if clash_member and clash_member["wxid"] != wxid:
                    return True
                has_kb = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_entities'").fetchone()
                if has_kb:
                    for row in db.execute("""SELECT name, aliases, wxid FROM knowledge_entities
                        WHERE account=? AND room=? AND entity_key LIKE 'person:%'""", (self.account, room)):
                        if row["wxid"] == wxid:
                            continue
                        aliases = {name_key(row["name"]), *parse_aliases(row["aliases"])}
                        if key in aliases:
                            return True
            return False

    def safe_wechat_name(self, wxid, room=None):
        """返回通过防冒充校验的微信昵称；若撞名则返回 None（仅记录在 wechat_names，不作为显示身份）。"""
        raw = self.wechat_name(wxid)
        if not raw:
            return None
        if room is not None and self.is_impersonating_name(wxid, room, raw):
            return None
        return raw

    def display_name(self, wxid, room):
        """群内绑定优先；未绑定时使用通过防冒充校验的微信昵称。"""
        member = self.find_by_wxid(wxid, room)
        return member.display_name if member else self.safe_wechat_name(wxid, room)

    def at_display_name(self, wxid, room):
        """群内 @ 气泡前缀优先使用真实微信昵称，无缓存时回退到群内绑定名。"""
        return self.wechat_name(wxid) or self.display_name(wxid, room)

    def sync_room_directory(self, room, member_wxids=(), wechat_names=None):
        if room not in self.groups:
            return
        now = self.now().astimezone(timezone.utc).isoformat()
        with self.transaction() as db:
            for wxid in member_wxids:
                validate_mentions(room, [wxid])
                db.execute("""INSERT INTO room_members(account,room,wxid,first_seen,last_seen) VALUES(?,?,?,?,?)
                    ON CONFLICT(account,room,wxid) DO UPDATE SET first_seen=COALESCE(room_members.first_seen,excluded.first_seen)""",
                    (self.account, room, wxid, now, now))
            for wxid, name in (wechat_names or {}).items():
                validate_mentions(room, [wxid])
                db.execute("""INSERT INTO room_members(account,room,wxid,first_seen,last_seen) VALUES(?,?,?,?,?)
                    ON CONFLICT(account,room,wxid) DO UPDATE SET first_seen=COALESCE(room_members.first_seen,excluded.first_seen)""",
                    (self.account, room, wxid, now, now))
                if name:
                    try:
                        self.remember_wechat_name(wxid, name)
                    except MemberError:
                        pass

    def room_member_directory(self, room):
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self.database.lock:
            db = self.connection()
            rows = db.execute("SELECT wxid FROM room_members WHERE account=? AND room=? ORDER BY wxid",
                              (self.account, room)).fetchall()
            directory = []
            for index, row in enumerate(rows, 1):
                wxid = row["wxid"]
                raw_name = self.wechat_name(wxid)
                member = self.find_by_wxid(wxid, room)
                impersonating = bool(raw_name and not member and self.is_impersonating_name(wxid, room, raw_name))
                directory.append({
                    "index": index,
                    "code": f"M{index}",
                    "wxid": wxid,
                    "wechat_name": raw_name,
                    "member": member,
                    "impersonating": impersonating,
                })
            return directory

    def resolve_member_target(self, room, token="", *, at_members=()):
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self.database.lock:
            targets = [wxid for wxid in at_members if wxid != self.account]
            if len(targets) > 1:
                raise MemberError("一次只能指定一位群成员")
            if len(targets) == 1:
                validate_mentions(room, [targets[0]])
                return targets[0]
            clean = (token or "").strip()
            if clean.startswith("@"):
                clean = clean[1:].strip()
            if not clean:
                raise MemberError("请指定群成员（可 @群友、填 M序号、当前微信昵称或 wxid）")
            directory = self.room_member_directory(room)
            if len(clean) >= 2 and clean[0] in ("M", "m") and clean[1:].isdigit():
                code = clean.upper()
                for entry in directory:
                    if entry["code"] == code:
                        return entry["wxid"]
                raise MemberError(f"未找到成员编号 {code}，请先发送 /成员 查看列表")
            for entry in directory:
                if entry["wxid"] == clean:
                    return entry["wxid"]
            if clean.startswith("wxid_"):
                validate_mentions(room, [clean])
                return clean
            key = name_key(clean)
            by_wechat = [entry for entry in directory if entry["wechat_name"] and name_key(entry["wechat_name"]) == key]
            if len(by_wechat) == 1:
                return by_wechat[0]["wxid"]
            if len(by_wechat) > 1:
                choices = "、".join(f"{e['code']}({e['wxid']})" for e in by_wechat)
                raise MemberError(f"微信昵称“{clean}”对应多位群成员：{choices}，请改用 M序号 指定")
            member = self.resolve(clean, room)
            if member and member.wxid:
                return member.wxid
            raise MemberError(f"未找到群成员“{clean}”，可使用 @群友、/成员 中的 M序号、微信昵称或 wxid")

    def _row(self, name, room=None):
        sql = """SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                 WHERE a.account=? AND a.alias=?"""
        values = [self.account, name_key(name)]
        if room is not None:
            sql += " AND a.room=?"
            values.append(room)
        rows = self.connection().execute(sql, values).fetchall()
        if not rows:
            # 未绑定也可按已核对的微信名查询，但必须确实在这个群说过话。
            query = """SELECT DISTINCT m.room,n.name,n.wxid FROM wechat_names n JOIN room_members m
                ON m.account=n.account AND m.wxid=n.wxid WHERE n.account=? AND n.name_key=?"""
            parameters = [self.account, name_key(name)]
            if room is not None:
                query += " AND m.room=?"
                parameters.append(room)
            names = self.connection().execute(query, parameters).fetchall()
            if len(names) > 1:
                raise MemberError(f"微信名 {name} 对应多个成员或群，请用本群绑定称呼查询")
            if not names:
                raise MemberError(f"未登记称呼：{name}")
            bound = self.connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                (self.account, names[0]["room"], names[0]["wxid"])).fetchone()
            if bound:
                return bound
            if self.is_impersonating_name(names[0]["wxid"], names[0]["room"], names[0]["name"]):
                raise MemberError(f"称呼 {name} 与群内人物或管理员重名且尚未绑定，请先由管理员绑定")
            return {"room": names[0]["room"], "name": names[0]["name"], "wxid": names[0]["wxid"],
                    "person_key": None, "display_name": names[0]["name"], "self_named": 0}
        if len(rows) != 1:
            raise MemberError(f"多个群都有 {name}，请指定群 ID")
        return rows[0]

    def resolve(self, name, room=None):
        with self.database.lock:
            return self._member(self._row(name, room))

    def list_members(self, room=None):
        with self.database.lock:
            sql, values = "SELECT * FROM people WHERE account=?", [self.account]
            if room is not None:
                sql += " AND room=?"
                values.append(room)
            return [self._member(row) for row in self.connection().execute(sql + " ORDER BY room,person_key", values).fetchall()]

    def find_by_wxid(self, wxid, room):
        with self.database.lock:
            row = self.connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                                             (self.account, room, wxid)).fetchone()
            return self._member(row) if row else None

    def bind(self, name, room, wxid):
        validate_mentions(room, [wxid])
        with self.database.lock:
            row = self._row(name, room)
            if row["person_key"] is None:
                raise MemberError("未找到这个成员称呼，请联系管理员检查人物绑定")
            if row["wxid"] and row["wxid"] != wxid:
                raise MemberError(f"{row['name']} 已绑定其他账号，未修改原绑定")
            other = self.connection().execute("SELECT name FROM people WHERE account=? AND room=? AND wxid=?",
                                                (self.account, room, wxid)).fetchone()
            if other and other["name"] != row["name"]:
                raise MemberError(f"这个微信账号已绑定为 {other['name']}，未修改原绑定")
            with self.transaction() as db:
                db.execute("UPDATE people SET wxid=? WHERE account=? AND room=? AND person_key=?",
                           (wxid, self.account, room, row["person_key"]))
            return self._member(self._row(name, room))

    def register(self, name, room, wxid):
        """本人注册新称呼或改称呼；身份、旧称呼和记录仍由同一个 ID 关联。"""
        alias = name_key(name)
        name = name.strip()
        validate_mentions(room, [wxid])
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self.database.lock:
            db = self.connection()
            by_name = db.execute("""SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                WHERE a.account=? AND a.room=? AND a.alias=?""", (self.account, room, alias)).fetchone()
            by_id = db.execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                               (self.account, room, wxid)).fetchone()
            if by_name and by_name["wxid"] and by_name["wxid"] != wxid:
                raise MemberError(f"称呼 {name} 已被其他成员绑定，请换一个昵称")
            if by_name and by_id and by_name["person_key"] != by_id["person_key"]:
                raise MemberError(f"称呼 {name} 已属于另一份成员资料，请换一个昵称")
            row = by_id or by_name
            key = row["person_key"] if row else "member_" + uuid.uuid4().hex
            display = name  # 本人 /绑定 的名字同时用于本群查询和艾特显示。
            with db:
                if row:
                    db.execute("UPDATE people SET name=?,display_name=?,wxid=?,self_named=1 WHERE account=? AND room=? AND person_key=?",
                               (name, display, wxid, self.account, room, key))
                else:
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid,self_named)
                        VALUES(?,?,?,?,?,?,1)""", (self.account, room, key, name, name, wxid))
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            return self._member(self._row(name, room))

    def binding_reminder_due(self, room, wxid):
        """未绑定成员七天只提醒一次；已绑定成员不再提醒。"""
        if self.find_by_wxid(wxid, room) is not None:
            return False
        with self.transaction() as db:
            now = parse_utc(self.now())
            row = db.execute("SELECT reminded_at FROM binding_reminders WHERE account=? AND room=? AND wxid=?",
                             (self.account, room, wxid)).fetchone()
            if row and now - parse_utc(row[0]) < timedelta(days=7):
                return False
            db.execute("INSERT OR REPLACE INTO binding_reminders VALUES(?,?,?,?)",
                       (self.account, room, wxid, iso_utc(now)))
            return True

    def is_admin(self, wxid):
        from ..authorization import LOCAL_EDITOR
        return wxid is LOCAL_EDITOR or bool(self.owner_wxid and wxid == self.owner_wxid)

