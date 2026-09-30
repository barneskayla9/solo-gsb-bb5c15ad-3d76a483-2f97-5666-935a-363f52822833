"""SQLite 连接管理与初始化。

使用 WAL 并开启外键约束；写操作通过 ``immediate_tx`` 先获取保留锁，
配合条件 UPDATE 保证「同一物品并发确认最多成功一次」。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """开启 IMMEDIATE 事务：进入即取写锁，避免并发写互相饿死。

    正常退出提交，异常回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(db_path: str | Path) -> None:
    """创建库文件并应用幂等建表脚本（即数据初始化）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(path)
    try:
        conn.executescript(ddl)
        _apply_migrations(conn)
    finally:
        conn.close()


# 幂等列迁移：CREATE TABLE IF NOT EXISTS 不会给旧库补列。
_COLUMN_MIGRATIONS = {
    "items": {
        "version": "ALTER TABLE items ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(ddl)
    _migrate_claims_withdrawn(conn)


def _migrate_claims_withdrawn(conn: sqlite3.Connection) -> None:
    """旧库升级：claims 增加 withdrawn 状态与 withdrawn_at 列。

    CHECK 约束与新列无法用 ALTER TABLE 直接修改/插入到指定位置，
    因此检测到旧结构时重建表并搬移全部数据；新库（建表脚本已含
     withdrawn_at）检测到列存在即跳过，保持幂等。
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
    if "withdrawn_at" in existing:
        return
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            ALTER TABLE claims RENAME TO claims_old;
            CREATE TABLE claims (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id      INTEGER NOT NULL REFERENCES items(id),
                answer       TEXT    NOT NULL,
                contact      TEXT    NOT NULL,
                token_hash   TEXT    NOT NULL UNIQUE,
                status       TEXT    NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending', 'selected', 'rejected', 'withdrawn')),
                created_at   TEXT    NOT NULL,
                decided_at   TEXT,
                withdrawn_at TEXT
            );
            INSERT INTO claims
                (id, item_id, answer, contact, token_hash, status, created_at, decided_at)
                SELECT id, item_id, answer, contact, token_hash, status, created_at, decided_at
                FROM claims_old;
            DROP TABLE claims_old;
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id)")
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if fk_on else 'OFF'}")
