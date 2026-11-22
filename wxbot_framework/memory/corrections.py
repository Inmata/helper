"""保留本群已核实纠错及其来源，为当前问法和历史问法提供可追溯记录。"""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class VerifiedFact:
    id: int
    scope: str
    subject_key: str
    subject: str
    relation: str
    kind: str
    statement: str
    names: tuple
    phrases: tuple
    effective_on: str
    observed_on: str
    source: dict
    status: str = "active"

    def material(self):
        material = {"编号": self.id, "对象": self.subject, "关系": self.relation, "范围": self.scope,
                    "类别": self.kind, "事实": self.statement, "状态": self.status,
                    "出处": self.source.get("网址") or self.source.get("消息编号")}
        if self.effective_on:
            material["生效日期"] = self.effective_on
        if self.observed_on:
            material["资料确认至"] = self.observed_on
        if self.source.get("适用范围"):
            material["适用范围"] = self.source["适用范围"]
        return material


class VerifiedCorrections:
    def __init__(self, members):
        self.members = members

    def _scope(self, room):
        return self.members.account, room

    def list(self, room, *, include_revoked=False):
        with self.members.database.lock:
            rows = self.members.connection().execute("""
                SELECT id,scope,subject_key,subject,relation,kind,statement,names,phrases,
                    effective_on,observed_on,source,status
                FROM verified_corrections WHERE account=? AND room=? AND (status<>'revoked' OR ?)
                ORDER BY observed_on DESC,id DESC""", (*self._scope(room), include_revoked)).fetchall()
        return [VerifiedFact(**{**dict(row), "names": tuple(json.loads(row["names"])),
                                "phrases": tuple(json.loads(row["phrases"])), "source": json.loads(row["source"])})
                for row in rows]

    def record(self, room, fields, correction_id):
        fact = VerifiedFact(id=0, **fields)
        identity = (*self._scope(room), fact.scope, fact.subject_key, fact.relation)
        with self.members.transaction() as db:
            cursor = db.execute("""INSERT INTO verified_corrections(
                account,room,scope,subject_key,relation,subject,kind,statement,names,phrases,effective_on,observed_on,
                source,correction_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,room,scope,subject_key,relation,effective_on) DO UPDATE SET
                    observed_on=excluded.observed_on,source=excluded.source,statement=excluded.statement,
                    names=excluded.names,phrases=excluded.phrases,correction_id=excluded.correction_id
                WHERE verified_corrections.status<>'revoked' AND (
                    excluded.observed_on>verified_corrections.observed_on OR
                    (excluded.scope='本群' AND excluded.observed_on=verified_corrections.observed_on
                     AND json_extract(excluded.source,'$.消息编号') >
                         json_extract(verified_corrections.source,'$.消息编号')))""",
                (*identity, fact.subject, fact.kind, fact.statement, json.dumps(fact.names, ensure_ascii=False),
                 json.dumps(fact.phrases, ensure_ascii=False), fact.effective_on, fact.observed_on,
                 json.dumps(fact.source, ensure_ascii=False), correction_id))
            current = db.execute("""SELECT id,status FROM verified_corrections
                WHERE account=? AND room=? AND scope=? AND subject_key=? AND relation=?
                    AND effective_on=?""", (*identity, fact.effective_on)).fetchone()
            if current["status"] == "revoked":
                return None
            if fact.kind == "状态" and cursor.rowcount:
                versions = db.execute("""SELECT id FROM verified_corrections
                    WHERE account=? AND room=? AND scope=? AND subject_key=? AND relation=? AND kind='状态'
                        AND status IN ('active','superseded') ORDER BY effective_on DESC,observed_on DESC,id DESC""",
                    identity).fetchall()
                db.executemany("UPDATE verified_corrections SET status=? WHERE id=?",
                               [("active" if index == 0 else "superseded", row["id"]) for index, row in enumerate(versions)])
        return current["id"]

    def revoke(self, room, number):
        with self.members.transaction() as db:
            return bool(db.execute("""UPDATE verified_corrections SET status='revoked'
                WHERE account=? AND room=? AND id=? AND status<>'revoked'""",
                (*self._scope(room), number)).rowcount)

    def used(self, room, source_ids):
        if not source_ids:
            return set()
        placeholders = ",".join("?" for _ in source_ids)
        with self.members.database.lock:
            return {row["fact_id"] for row in self.members.connection().execute(
                f"""SELECT fact_id FROM correction_reply_uses
                    WHERE account=? AND room=? AND source_id IN ({placeholders})""",
                (*self._scope(room), *source_ids))}

    def record_use(self, room, source_id, facts):
        if source_id and facts:
            with self.members.transaction() as db:
                db.executemany("INSERT OR IGNORE INTO correction_reply_uses VALUES(?,?,?,?)",
                               [(*self._scope(room), source_id, fact.id) for fact in facts])
