"""SQLite 资源所有者：唯一连接、共享可重入锁、可嵌套事务与备份。

业务存储不再各自持有连接和锁，而是显式接收同一个 Database 实例，
从而保证全进程只有一条连接、一把锁和一套提交边界。
"""
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def backup_before_migration(db, path):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables or ("chat_buffers" in tables and
                      "context_floor" in {row[1] for row in db.execute("PRAGMA table_info(chat_buffers)")}):
        return None
    return backup_database(db, path, "before-memory")


def backup_database(db, path, label="manual"):
    folder = Path(path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = folder / (Path(path).stem + "-" + label + "-" + stamp + ".sqlite3")
    with closing(sqlite3.connect(target)) as output:
        db.backup(output)
    return target


class Database:
    """一个数据库文件对应一个实例；连接、锁和事务边界都由它统一管理。"""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._db = None
        self._closed = False
        self._depth = 0

    @property
    def lock(self):
        return self._lock

    @property
    def closed(self):
        return self._closed

    def connection(self):
        """惰性打开并复用同一条连接；并发调用由锁保护。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            if self._db is not None:
                return self._db
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            db.row_factory = sqlite3.Row
            try:
                backup_before_migration(db, self.path)
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
            except BaseException:
                db.close()
                raise
            self._db = db
            return db

    @contextmanager
    def transaction(self):
        """可嵌套事务：内层加入外层，只有最外层提交或回滚。

        下层写方法只执行 SQL、不自行提交，异常沿调用栈抛出，
        避免半批结果被内层提前提交。
        """
        with self._lock:
            db = self.connection()
            outermost = self._depth == 0
            self._depth += 1
            try:
                yield db
            except BaseException:
                self._depth -= 1
                if outermost:
                    db.rollback()
                raise
            else:
                self._depth -= 1
                if outermost:
                    db.commit()

    def discard(self):
        """丢弃当前连接以便重试；不进入永久关闭状态。"""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._closed = True
