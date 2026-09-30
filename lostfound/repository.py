"""数据访问层：所有 SQL 均为参数化查询；关键写操作为条件式原子更新。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import security
from .db import immediate_tx
# 状态中文标签（单一事实来源，模板也使用）
ITEM_STATUS_LABELS = {"kept": "保管中", "removed": "已撤下", "returned": "已交还"}
CLAIM_STATUS_LABELS = {"pending": "待核对", "selected": "已获选", "rejected": "未获选", "withdrawn": "已撤回"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- 物品

def create_item(conn: sqlite3.Connection, public_desc: str, private_features: str) -> int:
    ts = now_iso()
    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO items (public_description, private_features, status, created_at, updated_at)"
            " VALUES (?, ?, 'kept', ?, ?)",
            (public_desc, private_features, ts, ts),
        )
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def list_public_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """访客列表：仅公开保管中物品，不暴露任何私密信息。

    has_selected 用于提示该物品已有人获选、不再接受新申请。
    """
    return conn.execute(
        """
        SELECT i.id, i.public_description, i.created_at,
               EXISTS (SELECT 1 FROM claims c
                        WHERE c.item_id = i.id AND c.status = 'selected') AS has_selected
        FROM items i
        WHERE i.status = 'kept'
        ORDER BY i.id DESC
        """
    ).fetchall()


def list_items_for_staff(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*,
               (SELECT COUNT(*) FROM claims c WHERE c.item_id = i.id) AS claim_count,
               (SELECT COUNT(*) FROM claims c
                 WHERE c.item_id = i.id AND c.status = 'pending') AS pending_count
        FROM items i
        ORDER BY i.id DESC
        """
    ).fetchall()


def set_item_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    with immediate_tx(conn):
        conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), item_id),
        )


def remove_item(conn: sqlite3.Connection, item_id: int, expect_version: int | None = None) -> None:
    """撤下物品：同时拒绝其全部待核对申请（撤下后不可再申请）。

    已交还不可撤下；已撤下重复操作为幂等无变化。

    状态判断与状态翻转合并为一条条件 UPDATE：条件在写入时针对最新已提交行
    求值，不依赖事务开始时可能已过期的读快照。``expect_version`` 为打开物品
    页时的 items.version（单调递增）：页面打开后发生过选定 / 改判（version 已
    推进）时撤下被拒绝，要求刷新确认——从而与并发的选定 / 改判之间恰好一方
    的状态变更生效。不传则只按实时状态判断（兼容其他调用方与旧启动方式）。
    """
    ts = now_iso()
    with immediate_tx(conn):
        sql = "UPDATE items SET status = 'removed', updated_at = ?" \
              " WHERE id = ? AND status = 'kept'"
        params: list[Any] = [ts, item_id]
        if expect_version is not None:
            sql += " AND version = ?"
            params.append(expect_version)
        cur = conn.execute(sql, params)
        if cur.rowcount == 1:
            conn.execute(
                "UPDATE claims SET status = 'rejected', decided_at = ?"
                " WHERE item_id = ? AND status = 'pending'",
                (ts, item_id),
            )
            return
        # 未更新：区分不存在 / 已交还 / 已撤下 / 版本过期（此处读取已在写锁后）
        row = conn.execute("SELECT status, version FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] == "returned":
            raise ValueError("该物品已交还，不能撤下")
        if row["status"] == "removed":
            return  # 幂等：重复撤下无变化
        if expect_version is not None and row["version"] != expect_version:
            raise ValueError("该物品在页面打开后已发生选定或改判，请刷新页面确认最新状态后再撤下")
        raise ValueError("物品状态已被其他操作改变，请刷新页面后重试")


def restore_item(conn: sqlite3.Connection, item_id: int) -> None:
    with immediate_tx(conn):
        conn.execute(
            "UPDATE items SET status = 'kept', updated_at = ? WHERE id = ? AND status = 'removed'",
            (now_iso(), item_id),
        )


# ----------------------------------------------------------------- 申请

def create_claim(
    conn: sqlite3.Connection, item_id: int, answer: str, contact: str
) -> str:
    """提交认领。仅当物品仍在保管且尚无人获选时接受；返回明文查询凭证（仅此一次）。"""
    token = security.new_query_token()
    token_hash = security.sha256_hex(token)
    ts = now_iso()
    with immediate_tx(conn):
        row = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] != "kept":
            raise ValueError("该物品已撤下或已交还，不再接受新的认领申请")
        blocked = conn.execute(
            "SELECT 1 FROM claims WHERE item_id = ? AND status = 'selected' LIMIT 1",
            (item_id,),
        ).fetchone()
        if blocked is not None:
            raise ValueError("该物品已确定获选人，不再接受新的认领申请")
        conn.execute(
            "INSERT INTO claims (item_id, answer, contact, token_hash, status, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (item_id, answer, contact, token_hash, ts),
        )
    return token


def get_claim_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description AS item_description, i.status AS item_status
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.token_hash = ?
        """,
        (security.sha256_hex(token),),
    ).fetchone()


def list_claims_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE item_id = ? ORDER BY id ASC", (item_id,)
    ).fetchall()


def get_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def select_claim(conn: sqlite3.Connection, claim_id: int, code_ttl_seconds: int, key: bytes) -> str:
    """选定一份认领并签发首个一次性交还码。

    原子性保证：
      1. 条件 UPDATE 仅在物品处于 kept 且申请仍为 pending 时生效；
      2. 同事务内将同一物品的其他 pending 申请标记为 rejected。
    并发下至多一个请求 rowcount=1，其余得到 None（未获选）。
    """
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        # item_id 关联不可变，锁前读取；状态判断一律走锁内条件式 UPDATE。
        row = conn.execute("SELECT item_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None:
            raise LookupError("申请不存在")
        item_id = row["item_id"]

        # 第一条写：物品守卫 + 版本推进。物品已撤下 / 已交还（含并发刚提交）时
        # rowcount=0，整体拒绝，不会与并发撤下双重生效。
        item_upd = conn.execute(
            "UPDATE items SET updated_at = ?, version = version + 1"
            " WHERE id = ? AND status = 'kept'",
            (ts, item_id),
        )
        if item_upd.rowcount != 1:
            raise ValueError("物品已撤下或已交还，无法选定认领")

        cur = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'pending' AND item_id = ?",
            (ts, claim_id, item_id),
        )
        if cur.rowcount != 1:
            existing = get_claim(conn, claim_id)
            if existing is not None and existing["status"] == "rejected":
                raise ValueError("该申请已因他人获选而标记为未获选")
            if existing is not None and existing["status"] == "withdrawn":
                raise ValueError("该申请已被申请人撤回，不能选定")
            if existing is not None and existing["status"] == "selected":
                raise ValueError("该申请已经处理，不能重复选定")
            raise ValueError("该申请状态已改变，请刷新页面后重试")

        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending' AND id <> ?",
            (ts, item_id, claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


def withdraw_claim_by_token(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    """访客凭原查询凭证撤回本人申请。

    返回 {'ok': True, ...} / {'ok': False, 'reason': ...}；幂等：对已撤回
    申请重复提交不重复写入，返回 already=True 供页面提示。

    规则（全部在单个 IMMEDIATE 事务内、按提交时的最新已提交状态裁决）：
      - 物品已交还：拒绝撤回任何申请（状态保持原样）；
      - 待核对 / 未获选申请撤回：仅把本人状态置为 withdrawn、记录撤回时间；
      - 已获选申请撤回：本人状态置为 withdrawn，原子作废其全部未核销交还码；
        物品仍保管中则把同物品其余「未获选」申请恢复为「待核对」（已主动
        撤回者不恢复），物品重新接受新申请；物品已撤下则不恢复、不开放提交；
      - 已撤下/已交还不会因撤回而翻转（物品状态从不被本操作改变）。

    并发互斥（与选定 / 改判 / 重发 / 撤下 / 核码交还）：
      - 事务以 BEGIN IMMEDIATE 取库级写锁，锁内读到的必为最新已提交状态；
      - 获选撤回先对物品做 ``status='kept'`` 条件守卫；未命中时锁内重读：
        物品已交还则整体拒绝，已撤下则仅作废码（不恢复申请）；随后再以
        ``claims.status='selected'`` 条件 UPDATE 翻转本人状态——改判先提交
        会把该申请改为 rejected，rowcount=0，撤回整体回滚，绝不出现双获选；
      - 是否恢复其余「未获选」申请只由锁内最新物品状态决定（撤下先提交时
        不恢复），不依赖锁前快照；
      - 待核对/未获选撤回以 ``status IN ('pending','rejected')`` 条件
        UPDATE 兜底，并发选定 / 改判先翻转状态时撤回整体回滚；
      - 失败方按锁内最新状态返回明确原因，不留下可用旧码或部分更新。
    """
    token_hash = security.sha256_hex(token.strip())
    ts = now_iso()
    result: dict[str, Any] = {"ok": False, "reason": "查询凭证不正确，请核对后重试。"}
    with immediate_tx(conn):
        # BEGIN IMMEDIATE 已取得库级保留锁：此处读取的一定是最新已提交状态，
        # 随后所有判断都靠锁内条件式 UPDATE 兜底，不依赖锁前快照。
        row = conn.execute(
            """
            SELECT c.id, c.status, i.id AS item_id, i.status AS item_status
            FROM claims c JOIN items i ON i.id = c.item_id
            WHERE c.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            return result
        claim_id, item_id, claim_status, item_status = (
            row["id"], row["item_id"], row["status"], row["item_status"],
        )

        # 重复撤回：幂等无写入，页面仍只显示「已撤回」。
        if claim_status == "withdrawn":
            return {
                "ok": True, "already": True, "claim_id": claim_id,
                "item_id": item_id, "item_status": item_status,
            }

        # 物品已交还：任何状态的申请都不得撤回。
        if item_status == "returned":
            return {
                "ok": False, "reason": "物品已交还，不能撤回认领申请。",
                "claim_id": claim_id, "item_id": item_id,
            }

        if claim_status == "selected":
            # 物品守卫：获选撤回只允许在物品仍保管中（未撤下、未交还）时，
            # 或物品已撤下（仅作废码）时生效。
            # 条件在写入时针对最新已提交行求值——并发核码先提交使物品变
            # returned 时 rowcount=0，撤回整体拒绝；撤下先提交时该守卫也
            # rowcount=0，随后锁内重读区分「已撤下（允许，仅作废码）」。
            kept_guard = conn.execute(
                "UPDATE items SET updated_at = ? WHERE id = ? AND status = 'kept'",
                (ts, item_id),
            )
            was_removed = False
            if kept_guard.rowcount != 1:
                cur_item = conn.execute(
                    "SELECT status FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                if cur_item is not None and cur_item["status"] == "removed":
                    # 物品已撤下：获选申请仍可撤回（只作废码、不恢复其他申请）。
                    was_removed = True
                elif cur_item is not None and cur_item["status"] == "returned":
                    return {
                        "ok": False, "reason": "物品已交还，不能撤回认领申请。",
                        "claim_id": claim_id, "item_id": item_id,
                    }
                else:
                    return {
                        "ok": False,
                        "reason": "物品状态已被其他操作改变，请刷新查询页确认最新状态后再撤回。",
                        "claim_id": claim_id, "item_id": item_id,
                    }

            # 关键互斥点：条件 UPDATE 要求申请此刻仍为 selected。
            # 并发改判先提交时该申请已变为 rejected（rowcount=0），撤回整体
            # 回滚，绝不与改判产生双状态变更 / 双获选 / 旧码残留。
            cur = conn.execute(
                "UPDATE claims SET status = 'withdrawn', withdrawn_at = ?"
                " WHERE id = ? AND status = 'selected'",
                (ts, claim_id),
            )
            if cur.rowcount != 1:
                latest = conn.execute(
                    "SELECT status FROM claims WHERE id = ?", (claim_id,)
                ).fetchone()
                if latest is not None and latest["status"] == "rejected":
                    return {
                        "ok": False,
                        "reason": "该申请的获选资格刚被工作人员改判，不能撤回；请刷新查询页确认最新状态。",
                        "claim_id": claim_id, "item_id": item_id,
                    }
                return {
                    "ok": False,
                    "reason": "申请状态已被其他操作改变，请刷新查询页确认最新状态后再撤回。",
                    "claim_id": claim_id, "item_id": item_id,
                }
            # 原子作废该申请全部未核销交还码（含已过期未核销者；已核销码不动）。
            conn.execute(
                "UPDATE handover_codes SET invalidated_at = ?"
                " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (ts, claim_id),
            )
            # 是否恢复其余未获选申请，必须以锁内最新物品状态裁决，不使用锁前
            # 快照：等锁期间撤下刚提交时（was_removed）不恢复、不开放提交。
            restored = 0
            if not was_removed:
                restored = conn.execute(
                    "UPDATE claims SET status = 'pending', decided_at = NULL"
                    " WHERE item_id = ? AND status = 'rejected'",
                    (item_id,),
                ).rowcount
            return {
                "ok": True, "already": False, "claim_id": claim_id,
                "item_id": item_id,
                "item_status": "removed" if was_removed else "kept",
                "was_selected": True, "restored": int(restored or 0),
            }

        # pending / rejected：撤回仅改变本人状态，不影响物品与其他申请。
        # 条件式 UPDATE 同时挡住并发选定（pending->selected）与并发改判
        # （rejected->selected）：先提交者翻转状态后，本语句 rowcount=0。
        cur = conn.execute(
            "UPDATE claims SET status = 'withdrawn', withdrawn_at = ?"
            " WHERE id = ? AND status IN ('pending', 'rejected')",
            (ts, claim_id),
        )
        if cur.rowcount != 1:
            latest = conn.execute(
                "SELECT status FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if latest is not None and latest["status"] == "selected":
                return {
                    "ok": False,
                    "reason": "该申请刚被工作人员选定为获选人，不能撤回；请刷新查询页确认最新状态。",
                    "claim_id": claim_id, "item_id": item_id,
                }
            return {
                "ok": False,
                "reason": "申请状态已被其他操作改变，请刷新查询页确认最新状态后再撤回。",
                "claim_id": claim_id, "item_id": item_id,
            }
        return {
            "ok": True, "already": False, "claim_id": claim_id,
            "item_id": item_id, "item_status": item_status,
            "was_selected": False, "restored": 0,
        }


# ----------------------------------------------------------------- 改判（误选更正）

def rejudge_claim(
    conn: sqlite3.Connection,
    from_claim_id: int,
    to_claim_id: int,
    reason: str,
    code_ttl_seconds: int,
    key: bytes,
    expect_item_version: int | None = None,
) -> None:
    """把获选资格从原申请改判给同一物品的另一份「未获选」申请。

    前置条件（任一不满足即拒绝且不改动任何状态）：
      - 原申请当前必须仍为 selected；
      - 目标申请必须存在、属于同一物品且当前为 rejected；
      - 物品必须仍为 kept（未撤下、未交还）；
      - 更正理由非空（由调用方裁剪后保证）。

    ``expect_item_version`` 为打开原申请页时物品的 version（单调递增整数）。
    物品行守卫 UPDATE 同时要求 ``status='kept' AND version=?`` 并推进版本，
    因此与并发撤下 / 选定 / 另一改判之间对称互斥：先提交者推进版本，后到者
    条件不匹配而整体回滚，只允许一次状态变更生效。不传则跳过版本校验
    （兼容其他调用方与旧启动方式）。

    成功后在单个 IMMEDIATE 事务内：
      1. 原申请 selected -> rejected，其全部未核销交还码立即失效；
      2. 目标申请 rejected -> selected，并签发一枚新的一次性交还码；
      3. 写入 corrections 留痕（双方、理由、时间）。
    """
    ts = now_iso()
    if not reason or not reason.strip():
        raise ValueError("更正理由不能为空")
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        # 锁前只读校验（此时连接在自动提交态，读到的是最新已提交数据）：
        # 申请双方必须存在、目标必须是同一物品的另一份 rejected 申请。
        # 锁内条件式 UPDATE 再兜底并发变化。
        src = conn.execute("SELECT id, item_id FROM claims WHERE id = ?", (from_claim_id,)).fetchone()
        if src is None:
            raise LookupError("原获选申请不存在")
        dst = conn.execute(
            "SELECT id, item_id, status FROM claims WHERE id = ?", (to_claim_id,)
        ).fetchone()
        if dst is None:
            raise ValueError("目标申请不存在")
        if dst["id"] == src["id"]:
            raise ValueError("改判目标不能是原申请本身")
        if dst["item_id"] != src["item_id"]:
            raise ValueError("目标申请不属于同一物品，不能改判")
        if dst["status"] != "rejected":
            raise ValueError("目标申请必须为未获选状态")

        # 第一条写：原子守卫 + 版本推进。条件在写入时针对最新已提交行求值；
        # 物品已撤下 / 已交还，或版本已被并发撤下 / 选定 / 改判推进时 rowcount=0。
        if expect_item_version is not None:
            item_upd = conn.execute(
                "UPDATE items SET updated_at = ?, version = version + 1"
                " WHERE id = ? AND status = 'kept' AND version = ?",
                (ts, src["item_id"], expect_item_version),
            )
        else:
            item_upd = conn.execute(
                "UPDATE items SET updated_at = ?, version = version + 1"
                " WHERE id = ? AND status = 'kept'",
                (ts, src["item_id"]),
            )
        if item_upd.rowcount != 1:
            cur = conn.execute("SELECT status FROM items WHERE id = ?", (src["item_id"],)).fetchone()
            if cur is None or cur["status"] != "kept":
                raise ValueError("物品已撤下或已交还，不能改判")
            raise ValueError("该物品状态刚被其他操作改变（撤下/选定/改判），请刷新页面确认后再改判")

        down = conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE id = ? AND status = 'selected' AND item_id = ?",
            (ts, from_claim_id, src["item_id"]),
        )
        if down.rowcount != 1:
            raise ValueError("原申请已非获选状态，不能改判")
        up = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'rejected' AND item_id = ?",
            (ts, to_claim_id, src["item_id"]),
        )
        if up.rowcount != 1:
            raise ValueError("目标申请必须为未获选状态")

        # 原获选人的未核销交还码（含已过期但未核销者）立即失效；已核销码保持原样。
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, from_claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (to_claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
        conn.execute(
            "INSERT INTO corrections (item_id, from_claim_id, to_claim_id, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (src["item_id"], from_claim_id, to_claim_id, reason, ts),
        )


_CORRECTION_SELECT = """
    SELECT cr.*,
           fc.answer AS from_answer, fc.contact AS from_contact,
           tc.answer AS to_answer,   tc.contact AS to_contact
    FROM corrections cr
    JOIN claims fc ON fc.id = cr.from_claim_id
    JOIN claims tc ON tc.id = cr.to_claim_id
"""


def list_corrections_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """该物品的全部改判记录（工作人员追溯），按时间正序。"""
    return conn.execute(
        _CORRECTION_SELECT + " WHERE cr.item_id = ? ORDER BY cr.id ASC", (item_id,)
    ).fetchall()


def list_corrections_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    """涉及某份申请（作为原申请或目标申请）的改判记录，按时间正序。"""
    return conn.execute(
        _CORRECTION_SELECT
        + " WHERE cr.from_claim_id = ? OR cr.to_claim_id = ? ORDER BY cr.id ASC",
        (claim_id, claim_id),
    ).fetchall()


# ----------------------------------------------------------------- 交还码

def get_active_code_for_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    """未核销、未作废旧码的最新一枚交还码（可能已过期）。"""
    return conn.execute(
        "SELECT * FROM handover_codes WHERE claim_id = ? AND consumed_at IS NULL"
        " AND invalidated_at IS NULL ORDER BY id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()


def get_claim_with_context(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description, i.private_features,
               i.status AS item_status, i.version AS item_version
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.id = ?
        """,
        (claim_id,),
    ).fetchone()


def resend_code(conn: sqlite3.Connection, claim_id: int, ttl_seconds: int, key: bytes) -> str:
    """重发交还码：旧码立即失效，签发一枚新码。

    守卫均为锁内条件式 UPDATE（不依赖等锁期间的旧读快照）：申请在签发时
    必须仍为 selected、物品未交还；否则整体拒绝。因此与并发改判（原申请变
    rejected）或核码交还（物品变 returned）之间，不会给已失效状态签发新码。
    """
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        claim = conn.execute("SELECT id, item_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            raise LookupError("申请不存在")
        guard = conn.execute(
            "UPDATE claims SET decided_at = decided_at WHERE id = ? AND status = 'selected'"
            " RETURNING id",
            (claim_id,),
        )
        if guard.fetchone() is None:
            cur = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if cur is not None and cur["status"] == "withdrawn":
                raise ValueError("该申请已被申请人撤回，不能重发交还码")
            raise ValueError("只有已获选的申请可以重发交还码")
        item_guard = conn.execute(
            "UPDATE items SET updated_at = updated_at WHERE id = ? AND status != 'returned'"
            " RETURNING id",
            (claim["item_id"],),
        )
        if item_guard.fetchone() is None:
            raise ValueError("物品已交还，无需重发")
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


def decrypt_code(key: bytes, code_row: sqlite3.Row) -> str | None:
    """授权页面回显交还码明文（数据库只存哈希与密文）。"""
    return security.decrypt_field(key, code_row["code_enc"])


def consume_code(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """工作人员核码并完成交还。

    单次 IMMEDIATE 事务内做存在性/状态判断与核销，保证：
      - 重复核码（同一码两次提交、两枚码并发核销）不得再次交还；
      - 已交还、已过期、已作废旧码均拒绝。
    返回 {'ok': True, 'claim_id': ..., 'item_id': ...} 或
    {'ok': False, 'reason': '...'}。
    """
    code_hash = security.sha256_hex(code.strip())
    ts = now_iso()
    result: dict[str, Any] = {"ok": False, "reason": "交还码不正确"}
    with immediate_tx(conn):
        hc = conn.execute(
            """
            SELECT hc.*, c.item_id, i.version AS item_version
            FROM handover_codes hc
            JOIN claims c ON c.id = hc.claim_id
            JOIN items i ON i.id = c.item_id
            WHERE hc.code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if hc is None:
            return result
        if hc["consumed_at"] is not None:
            return {"ok": False, "reason": "该交还码已使用，不能重复核销"}
        if hc["invalidated_at"] is not None:
            return {"ok": False, "reason": "该交还码已失效（已重发新码、已改判或获选申请已撤回）"}
        if hc["expires_at"] < ts:
            return {"ok": False, "reason": "该交还码已过期，请为获选人重发新码"}
        # 核销 UPDATE 同时要求码仍未核销 / 未作废、申请仍为 selected：
        # 若并发改判已将旧码作废或把该申请改为 rejected，则 rowcount=0。
        cur = conn.execute(
            "UPDATE handover_codes SET consumed_at = ?"
            " WHERE id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, hc["id"]),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "该交还码已失效或已被核销（并发请求）"}
        claim_guard = conn.execute(
            "UPDATE claims SET decided_at = decided_at WHERE id = ? AND status = 'selected'"
            " RETURNING id",
            (hc["claim_id"],),
        )
        if claim_guard.fetchone() is None:
            raise RuntimeError("申请状态已变更，回滚本次核销")
        upd = conn.execute(
            "UPDATE items SET status = 'returned', updated_at = ?"
            " WHERE id = ? AND status = 'kept' AND version = ?",
            (ts, hc["item_id"], hc["item_version"]),
        )
        if upd.rowcount != 1:
            raise RuntimeError("物品状态异常，回滚本次核销")
        result = {"ok": True, "claim_id": hc["claim_id"], "item_id": hc["item_id"]}
    return result
