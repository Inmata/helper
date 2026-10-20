"""启动公告队列与发送编排；每项本运行只自动尝试一次。

与 ``upgrades.py`` 的资料修订分开：这里只负责公告状态、尝试记录与发送。
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from ..plugin_engine.manifest import announcement_text
from ..timeutil import iso_utc

logger = logging.getLogger("Upgrade")


class StartupNotice:
    """旧版单公告兼容入口；运行时使用 AnnouncementQueue。"""

    def __init__(self, knowledge, service, options):
        self.knowledge, self.service, self.options = knowledge, service, options
        digest = hashlib.sha256(options["text"].encode("utf-8")).hexdigest()[:12]
        self.notice_id = f"{options['id']}#{digest}"
        # 单次运行只尝试一次；上次结果未确认时交给下次启动重试。
        self._attempted = False

    def status(self):
        k, o = self.knowledge, self.options
        with k.lock:
            row = k.connection().execute("SELECT status FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                                  (k.account, o["room"], self.notice_id)).fetchone()
            return row[0] if row else "pending"

    def describe(self):
        """维护命令用的可读状态：本版本公告 ID、目标群、结果与最近尝试时间。"""
        k, o = self.knowledge, self.options
        with k.lock:
            row = k.connection().execute("SELECT status,updated_at FROM startup_notices "
                                  "WHERE account=? AND room=? AND notice_id=?",
                                  (k.account, o["room"], self.notice_id)).fetchone()
        status, updated = (row["status"], row["updated_at"]) if row else ("pending", "")
        return {"id": self.notice_id, "room": o["room"], "status": status, "updated_at": updated}

    def retry(self):
        k, o = self.knowledge, self.options
        # 手动重发时允许同一次运行内再次尝试。
        self._attempted = False
        with k.lock, k.connection() as db:
            db.execute("DELETE FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                       (k.account, o["room"], self.notice_id))

    def send(self):
        k, o, session = self.knowledge, self.options, self.service.session_id
        if self._attempted:
            return False
        if not session:
            logger.info("启动公告暂缓：微信会话尚未就绪（%s）", self.notice_id)
            return False
        k._room(o["room"])
        with k.lock, k.connection() as db:
            # 已确认发送成功的版本不再重复；结果未确认的允许在下次启动重试。
            claimed = db.execute(
                "INSERT INTO startup_notices(account,room,notice_id,status,updated_at) VALUES(?,?,?,'sending',?) "
                "ON CONFLICT(account,room,notice_id) DO UPDATE SET status='sending',updated_at=excluded.updated_at "
                "WHERE startup_notices.status<>'sent'",
                (k.account, o["room"], self.notice_id, k.now())).rowcount
            if not claimed:
                logger.info("启动公告跳过：本版本已公告过（%s）", self.notice_id)
                return False
        self._attempted = True
        status = "unknown"
        logger.info("启动公告开始发送：%s -> %s", self.notice_id, o["room"])
        try:
            success = self.service.send_text(o["room"], o["text"], expected_session=session)
            status = "sent" if success else "unknown"
            if success:
                logger.info("启动公告已发送：%s -> %s", self.notice_id, o["room"])
            else:
                logger.warning("启动公告结果待确认，下次启动会重试：%s", self.notice_id)
            return bool(success)
        except Exception as error:
            logger.warning("启动公告发送失败，下次启动会重试：%s（%s）", self.notice_id, type(error).__name__)
            return False
        finally:
            with k.lock, k.connection() as db:
                db.execute("UPDATE startup_notices SET status=?,updated_at=? WHERE account=? AND room=? AND notice_id=?",
                           (status, k.now(), k.account, o["room"], self.notice_id))


class AnnouncementQueue:
    """按配置顺序尝试本次运行的公告；每项本运行只自动尝试一次。"""

    def __init__(self, knowledge, service, options, *, run_id=None, contributors=None):
        self.knowledge, self.service, self.run_id = knowledge, service, run_id or uuid.uuid4().hex
        self.notices = tuple(options or ())
        self.contributors = contributors
        self._successful = set()
        self._attempted = set()
        for notice in self.notices:
            knowledge._room(notice["room"])

    def notice(self, notice_id=None):
        if notice_id is None:
            if len(self.notices) != 1:
                raise ValueError("配置了多份公告时必须指定公告 ID")
            return self.notices[0]
        matched = [notice for notice in self.notices if notice["id"] == notice_id]
        if not matched:
            raise ValueError(f"未找到公告：{notice_id}")
        return matched[0]

    def status(self, notice_id=None):
        """返回本运行与最近一次历史尝试，不把旧版本的 sent 当成本次已发送。"""
        notice = self.notice(notice_id)
        k = self.knowledge
        with k.lock:
            db = k.connection()
            latest = db.execute("""SELECT * FROM startup_notice_attempts
                WHERE account=? AND room=? AND notice_id=? ORDER BY id DESC LIMIT 1""",
                (k.account, notice["room"], notice["id"])).fetchone()
            current = db.execute("""SELECT * FROM startup_notice_attempts
                WHERE account=? AND room=? AND notice_id=? AND run_id=? ORDER BY id DESC LIMIT 1""",
                (k.account, notice["room"], notice["id"], self.run_id)).fetchone()
        local = self.service.session_id is not None
        status = current["status"] if current else ("sent" if notice["id"] in self._successful
                                                     else "pending" if local else "waiting")
        return {
            "id": notice["id"],
            "room": notice["room"],
            "plugins": tuple(notice.get("plugins", ())),
            "status": status,
            "attempted_at": current["attempted_at"] if current else None,
            "updated_at": current["updated_at"] if current else None,
            "last_attempt": dict(latest) if latest else None,
            "run_id": self.run_id,
        }

    def describe(self, notice_id=None):
        return self.status(notice_id)

    def all_status(self):
        return [self.status(notice["id"]) for notice in self.notices]

    def _record_attempt(self, notice, status, attempted_at):
        k = self.knowledge
        timestamp = iso_utc(attempted_at)
        with k.lock, k.connection() as db:
            row = db.execute("""INSERT INTO startup_notice_attempts
                (account,room,notice_id,run_id,status,attempted_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (k.account, notice["room"], notice["id"], self.run_id, status,
                 timestamp, timestamp))
            db.execute("""INSERT INTO startup_notices(account,room,notice_id,status,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(account,room,notice_id) DO UPDATE SET
                status=excluded.status,updated_at=excluded.updated_at""",
                (k.account, notice["room"], notice["id"], status, timestamp))
            return row.lastrowid

    def _send_one(self, notice):
        k, session = self.knowledge, self.service.session_id
        if notice["id"] in self._successful or notice["id"] in self._attempted:
            return False
        if not session:
            logger.info("公告暂缓：微信会话尚未就绪（%s）", notice["id"])
            return False
        k._room(notice["room"])
        self._attempted.add(notice["id"])
        attempted_at = iso_utc(k.members.now())
        attempt_id = self._record_attempt(notice, "sending", attempted_at)
        status = "unknown"
        logger.info("公告开始发送：%s -> %s（run=%s）", notice["id"], notice["room"], self.run_id)
        try:
            success = self.service.send_text(
                notice["room"], announcement_text(notice, self.contributors), expected_session=session)
            status = "sent" if success else "unknown"
            if success:
                self._successful.add(notice["id"])
                logger.info("公告已发送：%s -> %s", notice["id"], notice["room"])
            else:
                logger.warning("公告结果待确认：%s", notice["id"])
            return bool(success)
        except Exception as error:
            logger.warning("公告发送失败：%s（%s）", notice["id"], type(error).__name__)
            return False
        finally:
            completed = iso_utc(k.members.now())
            with k.lock, k.connection() as db:
                db.execute("""UPDATE startup_notice_attempts SET status=?,updated_at=?
                    WHERE id=?""", (status, completed, attempt_id))
                db.execute("""UPDATE startup_notices SET status=?,updated_at=?
                    WHERE account=? AND room=? AND notice_id=?""",
                    (status, completed, k.account, notice["room"], notice["id"]))

    def send(self, notice_id=None):
        """一次调用只发送指定公告，或依次发送所有尚未成功的公告。"""
        targets = (self.notice(notice_id),) if notice_id is not None else self.notices
        sent = False
        for notice in targets:
            if self.service.session_id is None:
                break
            sent = self._send_one(notice) or sent
        return sent

    def resend(self, notice_id=None):
        """维护员明确重发时，允许本次运行追加一条独立发送记录。"""
        notice = self.notice(notice_id)
        self._attempted.discard(notice["id"])
        self._successful.discard(notice["id"])
        return self._send_one(notice)


def migrate_legacy_notice(notice):
    if notice is None:
        return None
    return {"id": notice["id"], "room": notice["room"], "plugins": [], "text": notice["text"]}
