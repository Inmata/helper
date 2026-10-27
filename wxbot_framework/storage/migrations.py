"""集中的表结构与既有迁移；保持原表、主键、来源与升级次序不变。"""
from __future__ import annotations

from .database import backup_before_migration, backup_database

BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS people (
        account TEXT NOT NULL, room TEXT NOT NULL, person_key TEXT NOT NULL,
        name TEXT NOT NULL, display_name TEXT NOT NULL, wxid TEXT,
        PRIMARY KEY(account, room, person_key), UNIQUE(account, room, wxid));
    CREATE TABLE IF NOT EXISTS aliases (
        account TEXT NOT NULL, room TEXT NOT NULL, alias TEXT NOT NULL, person_key TEXT NOT NULL,
        PRIMARY KEY(account, room, alias),
        FOREIGN KEY(account, room, person_key) REFERENCES people(account, room, person_key));
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, sender_id TEXT NOT NULL,
        text TEXT NOT NULL, received_at TEXT NOT NULL, session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, is_self INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS messages_by_member ON messages(account, room, sender_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS messages_by_sequence
        ON messages(account, session_id, sequence) WHERE sequence > 0;
    CREATE TABLE IF NOT EXISTS chat_buffers (
        account TEXT NOT NULL, room TEXT NOT NULL, local_day TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,room));
    CREATE TABLE IF NOT EXISTS room_personalities (
        account TEXT NOT NULL, room TEXT NOT NULL, name TEXT NOT NULL,
        generation INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,room));
    -- 人格补充版本：按群递增留存，生效版本记在 room_personalities.generation（0 表示基准）。
    CREATE TABLE IF NOT EXISTS persona_versions (
        account TEXT NOT NULL, room TEXT NOT NULL, version INTEGER NOT NULL,
        supplement TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY(account,room,version));
    -- 最近一次归纳尝试（含失败与内容未变），供调度间隔判断。
    CREATE TABLE IF NOT EXISTS persona_runs (
        account TEXT NOT NULL, room TEXT NOT NULL, attempted_at TEXT NOT NULL,
        status TEXT NOT NULL, PRIMARY KEY(account,room));
    CREATE TABLE IF NOT EXISTS wechat_names (
        account TEXT NOT NULL, wxid TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
        PRIMARY KEY(account,wxid));
    CREATE TABLE IF NOT EXISTS room_members (
        account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
        PRIMARY KEY(account,room,wxid));
    INSERT OR IGNORE INTO room_members(account,room,wxid) SELECT DISTINCT account,room,sender_id FROM messages;
"""

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_entities (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, entity_key TEXT NOT NULL,
    name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', wxid TEXT, pinned INTEGER NOT NULL DEFAULT 0,
    epoch INTEGER NOT NULL DEFAULT 0, learning_floor INTEGER NOT NULL DEFAULT 0,
    aliases_manual INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
    UNIQUE(account,room,entity_key));
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_member ON knowledge_entities(account,room,wxid) WHERE wxid IS NOT NULL;
CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, entity_id INTEGER NOT NULL,
    slot TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active', manual INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account,room,entity_id,epoch,slot));
CREATE TABLE IF NOT EXISTS knowledge_sources (
    item_id INTEGER NOT NULL, origin TEXT NOT NULL, message_id INTEGER, speaker_id TEXT,
    source_time TEXT NOT NULL, PRIMARY KEY(item_id,origin));
CREATE TABLE IF NOT EXISTS knowledge_mentions (
    entity_id INTEGER NOT NULL, message_id INTEGER NOT NULL, speaker_id TEXT NOT NULL,
    PRIMARY KEY(entity_id,message_id));
CREATE TABLE IF NOT EXISTS knowledge_edits (
    id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, editor TEXT NOT NULL, action TEXT NOT NULL,
    previous TEXT NOT NULL, replacement TEXT NOT NULL, edited_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_cursors (
    account TEXT NOT NULL, room TEXT NOT NULL, cursor_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT, last_error TEXT, PRIMARY KEY(account,room));
CREATE TABLE IF NOT EXISTS member_styles (
    entity_id INTEGER PRIMARY KEY, last_message_id INTEGER NOT NULL, sample_count INTEGER NOT NULL,
    average_length REAL NOT NULL, source_ids TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hourly_runs (
    account TEXT NOT NULL, room TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '', anchor_id INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(account,room,slot));
CREATE TABLE IF NOT EXISTS knowledge_alias_blocks (
    account TEXT NOT NULL, room TEXT NOT NULL, entity_id INTEGER NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY(account,room,entity_id,alias));
CREATE TABLE IF NOT EXISTS knowledge_entity_edits (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, entity_id INTEGER NOT NULL,
    editor TEXT NOT NULL, action TEXT NOT NULL, previous TEXT NOT NULL, replacement TEXT NOT NULL,
    edited_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_confirmations (
    code TEXT PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, editor TEXT NOT NULL,
    entity_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS applied_upgrades (
    account TEXT NOT NULL, upgrade_id TEXT NOT NULL, applied_at TEXT NOT NULL,
    PRIMARY KEY(account,upgrade_id));
CREATE TABLE IF NOT EXISTS startup_notices (
    account TEXT NOT NULL, room TEXT NOT NULL, notice_id TEXT NOT NULL, status TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(account,room,notice_id));
CREATE TABLE IF NOT EXISTS startup_notice_attempts (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, notice_id TEXT NOT NULL,
    run_id TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS startup_notice_attempts_by_notice
    ON startup_notice_attempts(account,room,notice_id,id);
CREATE TABLE IF NOT EXISTS proactive_candidates (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, run_id TEXT NOT NULL,
    due_at TEXT NOT NULL, status TEXT NOT NULL, anchor_id INTEGER NOT NULL,
    context_generation INTEGER NOT NULL, activity_count INTEGER NOT NULL DEFAULT 0,
    speaker_count INTEGER NOT NULL DEFAULT 0, topic TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT);
CREATE INDEX IF NOT EXISTS proactive_candidates_by_room
    ON proactive_candidates(account,room,id);
CREATE INDEX IF NOT EXISTS proactive_candidates_by_status
    ON proactive_candidates(account,room,run_id,status);
"""


def initialize_base_schema(db):
    """成员目录、聊天原文与群状态的公共表；可重复执行。"""
    db.executescript(BASE_SCHEMA)
    if "self_named" not in {row["name"] for row in db.execute("PRAGMA table_info(people)")}:
        db.execute("ALTER TABLE people ADD COLUMN self_named INTEGER NOT NULL DEFAULT 0")
    if "bot_generated" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
        db.execute("ALTER TABLE messages ADD COLUMN bot_generated INTEGER NOT NULL DEFAULT 0")
    if "context_floor" not in {row["name"] for row in db.execute("PRAGMA table_info(chat_buffers)")}:
        db.execute("ALTER TABLE chat_buffers ADD COLUMN context_floor INTEGER NOT NULL DEFAULT 0")
    # revision：每次切换生效版（含回退、回基准）单调加一，用来识别 1→2→1 这类往返切换。
    if "revision" not in {row["name"] for row in db.execute("PRAGMA table_info(room_personalities)")}:
        db.execute("ALTER TABLE room_personalities ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    for column in ("first_seen", "last_seen"):
        if column not in {row["name"] for row in db.execute("PRAGMA table_info(room_members)")}:
            db.execute(f"ALTER TABLE room_members ADD COLUMN {column} TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS binding_reminders (
        account TEXT, room TEXT, wxid TEXT, reminded_at TEXT,
        PRIMARY KEY(account,room,wxid))""")


def initialize_memory_schema(db, path):
    """旧条目编号与来源不变；一次性升级条目的唯一键以支持清空后重新学习。"""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables:
        # 新表可增量创建；原公告表与整点记录保留，供旧数据和回滚兼容。
        db.executescript(MEMORY_SCHEMA)
    if "knowledge_items" in tables and "epoch" not in {r[1] for r in db.execute("PRAGMA table_info(knowledge_items)")}:
        backup_database(db, path, "before-maintenance")
        with db:
            for column in ("epoch", "learning_floor", "aliases_manual"):
                if column not in {r[1] for r in db.execute("PRAGMA table_info(knowledge_entities)")}:
                    db.execute(f"ALTER TABLE knowledge_entities ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE knowledge_items_v2 (
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, entity_id INTEGER NOT NULL,
                slot TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active', manual INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account,room,entity_id,epoch,slot))""")
            db.execute("INSERT INTO knowledge_items_v2 SELECT *,0 FROM knowledge_items")
            db.execute("DROP TABLE knowledge_items")
            db.execute("ALTER TABLE knowledge_items_v2 RENAME TO knowledge_items")
    db.executescript(MEMORY_SCHEMA)
    if "knowledge_entities" in tables:
        columns = {r[1] for r in db.execute("PRAGMA table_info(knowledge_entities)")}
        if "status" not in columns:
            backup_database(db, path, "before-entity-status")
            with db:
                db.execute("ALTER TABLE knowledge_entities ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        _adopt_group_entity(db)


def initialize_database(db, path):
    """完整建库：基础表加记忆表，按原次序执行既有迁移。"""
    initialize_base_schema(db)
    initialize_memory_schema(db, path)


def _adopt_group_entity(db):
    """把历史误建成人物“本群”的公共记忆迁到独立的群主体，保留编号、来源与修订历史。"""
    legacy = db.execute("SELECT * FROM knowledge_entities WHERE entity_key='person:本群'").fetchall()
    if not legacy:
        return
    with db:
        for row in db.execute("SELECT id FROM knowledge_entities WHERE entity_key='group:本群'"):
            _drop_duplicate_items(db, row["id"])
        for row in legacy:
            existing = db.execute(
                "SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key='group:本群'",
                (row["account"], row["room"])).fetchone()
            if existing is None:
                db.execute("UPDATE knowledge_entities SET entity_key='group:本群',pinned=1 WHERE id=?", (row["id"],))
                _drop_duplicate_items(db, row["id"])
                continue
            for item in db.execute("SELECT * FROM knowledge_items WHERE entity_id=?", (row["id"],)).fetchall():
                conflict = db.execute("SELECT 1 FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                                      (existing["id"], item["epoch"], item["slot"])).fetchone()
                slot = item["slot"] if not conflict else item["slot"][:60] + "#" + str(item["id"])
                db.execute("UPDATE knowledge_items SET entity_id=?,slot=? WHERE id=?",
                           (existing["id"], slot, item["id"]))
            db.execute("UPDATE OR IGNORE knowledge_mentions SET entity_id=? WHERE entity_id=?",
                       (existing["id"], row["id"]))
            db.execute("DELETE FROM knowledge_mentions WHERE entity_id=?", (row["id"],))
            db.execute("UPDATE OR REPLACE member_styles SET entity_id=? WHERE entity_id=?",
                       (existing["id"], row["id"]))
            db.execute("DELETE FROM member_styles WHERE entity_id=?", (row["id"],))
            db.execute("DELETE FROM knowledge_entities WHERE id=?", (row["id"],))
            _drop_duplicate_items(db, existing["id"])


def _drop_duplicate_items(db, entity_id):
    """删除同一主体下内容完全相同、只是槽位被加上 #编号 后缀的重复条目。"""
    removed = []
    for item in db.execute("SELECT * FROM knowledge_items WHERE entity_id=?", (entity_id,)).fetchall():
        if "#" not in item["slot"]:
            continue
        base = item["slot"].rsplit("#", 1)[0]
        twin = db.execute("""SELECT * FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=? AND id<>?
            AND content=? AND manual=? AND status=?""",
            (entity_id, item["epoch"], base, item["id"], item["content"], item["manual"], item["status"])).fetchone()
        if twin is None:
            continue
        db.execute("DELETE FROM knowledge_sources WHERE item_id=?", (item["id"],))
        db.execute("DELETE FROM knowledge_edits WHERE item_id=?", (item["id"],))
        db.execute("DELETE FROM knowledge_items WHERE id=?", (item["id"],))
        removed.append(item["id"])
    return removed
