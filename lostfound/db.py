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

# 旧库 claims.status 的 CHECK 约束缺少 'withdrawn'（访客撤回）取值，
# ALTER 无法修改 CHECK，只能在空事务后以「建新表 -> 拷数据 -> 换表」方式幂等重建。
_CLAIMS_REBUILD_SQL = [
    "CREATE TABLE claims_new ("
    "    id         INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    item_id    INTEGER NOT NULL REFERENCES items(id),"
    "    answer     TEXT    NOT NULL,"
    "    contact    TEXT    NOT NULL,"
    "    token_hash TEXT    NOT NULL UNIQUE,"
    "    status     TEXT    NOT NULL DEFAULT 'pending'"
    "                 CHECK (status IN ('pending', 'selected', 'rejected', 'withdrawn')),"
    "    created_at TEXT    NOT NULL,"
    "    decided_at TEXT,"
    "    withdrawn_at TEXT"
    ")",
    "INSERT INTO claims_new (id, item_id, answer, contact, token_hash, status,"
    "                         created_at, decided_at, withdrawn_at)"
    " SELECT id, item_id, answer, contact, token_hash, status,"
    "        created_at, decided_at, withdrawn_at FROM claims",
    "DROP TABLE claims",
    "ALTER TABLE claims_new RENAME TO claims",
    "CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id)",
]


def _claims_needs_rebuild(conn: sqlite3.Connection) -> bool:
    """旧库 claims.status 的 CHECK 是否已含 'withdrawn' 枚举值。

    注意：必须匹配带引号的枚举字面量 ``'withdrawn'``；仅凭子串 ``withdrawn``
    会把 ALTER 新增的 ``withdrawn_at`` 列名误判为已迁移。
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if ddl is None:  # 全新库或尚无该表：schema.sql 已建为新结构，无需迁移
        return False
    return "'withdrawn'" not in (ddl[0] or "")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(ddl)

    # claims 列迁移（须在可能的 CHECK 重建之前完成，重建 SQL 会引用 withdrawn_at）
    claim_cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
    if claim_cols and "withdrawn_at" not in claim_cols:
        conn.execute("ALTER TABLE claims ADD COLUMN withdrawn_at TEXT")

    if _claims_needs_rebuild(conn):
        # 重建期间临时关闭外键（PRAGMA foreign_keys 不允许在事务内切换）：
        # 仅同库换表、不触碰引用方，完成后立即恢复。
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            for stmt in _CLAIMS_REBUILD_SQL:
                conn.execute(stmt)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

