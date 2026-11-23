from __future__ import annotations
from datetime import datetime
from fastapi import HTTPException
from app.config import TICKET_STATUSES


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def business_day(conn) -> str:
    return get_setting(conn, "营业日")


def is_sealed(conn, day: str | None = None) -> bool:
    d = day or business_day(conn)
    row = conn.execute(
        "SELECT sealed FROM day_seals WHERE business_day=?", (d,)
    ).fetchone()
    return bool(row and row["sealed"])


def require_open_day(conn) -> str:
    day = business_day(conn)
    if is_sealed(conn, day):
        raise HTTPException(400, detail="当日队列已封口，禁止取号与叫号")
    return day


# 开放窗口的唯一谓词：发号写入侧与所有读路径（等候墙、总览、窗口页、回执）共用。
# 计数口径：windows 表中 open=1 的行，与窗口绑定何种业务种类无关。
OPEN_WINDOW_PREDICATE = "open=1"


def open_window_count(conn) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM windows WHERE {OPEN_WINDOW_PREDICATE}"
    ).fetchone()
    return int(row["c"])


def open_window_names(conn) -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM windows WHERE {OPEN_WINDOW_PREDICATE} ORDER BY id"
    ).fetchall()
    return [r["name"] for r in rows]


def issue_gate(conn) -> dict:
    """发号闸快照：读路径与发号回执/失败都以此为准，写入侧用同一谓词重算。"""
    day = business_day(conn)
    sealed = is_sealed(conn, day)
    n = open_window_count(conn)
    return {
        "营业日": day,
        "已封口": sealed,
        "开放窗口": n,
        "可发号": (not sealed) and n > 0,
        "原因": "当日已封口" if sealed else ("无开放窗口" if n == 0 else ""),
    }


def ticket_row(conn, tid: int):
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="号牌不存在")
    return row


def window_row(conn, wid: int):
    row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="窗口不存在")
    return row


def kind_row(conn, kid: int):
    row = conn.execute("SELECT * FROM business_kinds WHERE id=?", (kid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="业务种类不存在")
    return row


def parse_kind_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    return [int(x) for x in raw.split(",") if x.strip()]


def begin_immediate(conn) -> None:
    """以写锁开启事务，令发号闸判定与写库之间不被并发收档插队。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def require_status(status: str) -> str:
    if status not in TICKET_STATUSES:
        raise HTTPException(400, detail="号牌状态不合法")
    return status


def ticket_dict(conn, row) -> dict:
    kind = conn.execute(
        "SELECT name, prefix FROM business_kinds WHERE id=?", (row["kind_id"],)
    ).fetchone()
    win = None
    if row["window_id"]:
        w = conn.execute("SELECT name FROM windows WHERE id=?", (row["window_id"],)).fetchone()
        win = w["name"] if w else None
    return {
        "id": row["id"],
        "号牌": row["slip_no"],
        "业务种类": kind["name"] if kind else "",
        "前缀": kind["prefix"] if kind else "",
        "营业日": row["business_day"],
        "状态": row["status"],
        "窗口": win,
        "窗口编号": row["window_id"],
        "位次": row["queue_pos"],
        "取号时刻": row["taken_at"],
        "叫号时刻": row["called_at"],
        "办结时刻": row["finished_at"],
        "备注": row["note"],
    }
