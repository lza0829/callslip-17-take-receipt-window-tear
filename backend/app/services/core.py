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


def issue_integrity(conn, day: str | None = None) -> dict:
    """成功灯重放：成功流水行 ∪ 号牌行 ∪ 号段游标 必须同命运。

    枚举当日每一笔「成功」发号，要求：
    - 号牌行仍在，且号牌号、营业日、业务种类与流水一致；
    - 游标推进恰为一格（seq_after == seq_before + 1）；
    - 流水记录了开放窗口证据（open_windows >= 1，且与窗口名快照一致）；
    - 种类的当前游标等于该种类成功流水里最大的 seq_after。
    任一不成立即记一条缺口，读路径据此不得宣称已发号。
    """
    d = day or business_day(conn)
    gaps: list[dict] = []
    rows = conn.execute(
        """SELECT e.id, e.kind_id, e.slip_no, e.ticket_id, e.open_windows,
                  e.open_window_names, e.seq_before, e.seq_after,
                  t.id AS t_id, t.slip_no AS t_slip, t.business_day AS t_day,
                  t.kind_id AS t_kind
           FROM issue_events e
           LEFT JOIN tickets t ON t.id = e.ticket_id
           WHERE e.business_day=? AND e.outcome='成功'
           ORDER BY e.id""",
        (d,),
    ).fetchall()
    for r in rows:
        if r["ticket_id"] is None or r["t_id"] is None:
            gaps.append({"事件": r["id"], "类型": "成功无号牌行", "号牌": r["slip_no"]})
            continue
        if r["t_slip"] != r["slip_no"]:
            gaps.append({"事件": r["id"], "类型": "流水号牌与号牌行不符", "号牌": r["slip_no"]})
        if r["t_day"] != d:
            gaps.append({"事件": r["id"], "类型": "号牌跨营业日", "号牌": r["slip_no"]})
        if r["t_kind"] != r["kind_id"]:
            gaps.append({"事件": r["id"], "类型": "流水种类与号牌行不符", "号牌": r["slip_no"]})
        if int(r["seq_after"]) != int(r["seq_before"]) + 1:
            gaps.append({"事件": r["id"], "类型": "游标推进非一格", "号牌": r["slip_no"]})
        if int(r["open_windows"]) < 1:
            gaps.append({"事件": r["id"], "类型": "成功流水无开放窗证据", "号牌": r["slip_no"]})
        elif r["open_window_names"]:
            names = [n for n in r["open_window_names"].split(",") if n]
            if len(names) != int(r["open_windows"]):
                gaps.append(
                    {"事件": r["id"], "类型": "开放窗名快照与计数不符", "号牌": r["slip_no"]}
                )
    # 旧版旁路留下的伪造成功注记不得冒充成功
    for r in conn.execute(
        "SELECT id, slip_no FROM issue_events WHERE business_day=? AND outcome='成功注记'",
        (d,),
    ).fetchall():
        gaps.append({"事件": r["id"], "类型": "伪造成功注记", "号牌": r["slip_no"]})
    for k in conn.execute("SELECT id, name, seq FROM business_kinds ORDER BY id").fetchall():
        exp = conn.execute(
            """SELECT MAX(seq_after) AS m FROM issue_events
               WHERE business_day=? AND outcome='成功' AND kind_id=?""",
            (d, k["id"]),
        ).fetchone()["m"]
        if exp is not None and int(k["seq"]) != int(exp):
            gaps.append(
                {
                    "类型": "号段游标与成功流水不符",
                    "业务种类": k["name"],
                    "当前游标": int(k["seq"]),
                    "流水游标": int(exp),
                }
            )
    return {
        "营业日": d,
        "完整": not gaps,
        "成功笔数": len(rows),
        "缺口": gaps,
    }


def verify_issue_replay(
    conn,
    *,
    day: str,
    kind_id: int,
    ticket_id: int,
    slip_no: str,
    seq_before: int,
    seq_after: int,
    open_windows: int,
    open_window_names: list[str],
) -> list[str]:
    """写侧同命运校验：落号后、提交前，成功灯必须能被四表重放出来。

    返回问题清单；非空即由调用方抛错，令整笔发号（含成功流水行）回滚。
    """
    problems: list[str] = []
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        problems.append("号牌行缺失")
    else:
        if row["slip_no"] != slip_no:
            problems.append("号牌号与回执不符")
        if row["business_day"] != day:
            problems.append("号牌营业日不符")
        if int(row["kind_id"]) != int(kind_id):
            problems.append("号牌业务种类不符")
    if int(seq_after) != int(seq_before) + 1:
        problems.append("游标推进非一格")
    cur = conn.execute(
        "SELECT seq FROM business_kinds WHERE id=?", (kind_id,)
    ).fetchone()
    if cur is None or int(cur["seq"]) != int(seq_after):
        problems.append("号段游标未落到回执值")
    if int(open_windows) < 1:
        problems.append("回执无开放窗证据")
    if len(open_window_names) != int(open_windows):
        problems.append("开放窗名快照与计数不符")
    ev = conn.execute(
        """SELECT outcome, ticket_id FROM issue_events
           WHERE business_day=? AND kind_id=? AND slip_no=?
           ORDER BY id DESC LIMIT 1""",
        (day, kind_id, slip_no),
    ).fetchone()
    if ev is None or ev["outcome"] != "成功" or ev["ticket_id"] != ticket_id:
        problems.append("成功流水行未与号牌同命运")
    return problems


def issue_gate(conn) -> dict:
    """发号闸快照：读路径与发号回执/失败都以此为准，写入侧用同一谓词重算。

    可发号 = 未封口 ∧ 有开放窗 ∧ 成功灯四表可重放（无缺口）。
    缺口未清前一律不得宣称已发号，也不得再叠加新的成功灯。
    """
    day = business_day(conn)
    sealed = is_sealed(conn, day)
    n = open_window_count(conn)
    integ = issue_integrity(conn, day)
    if sealed:
        reason = "当日已封口"
    elif n == 0:
        reason = "无开放窗口"
    elif not integ["完整"]:
        reason = "发号证据缺口未清"
    else:
        reason = ""
    return {
        "营业日": day,
        "已封口": sealed,
        "开放窗口": n,
        "可发号": (not sealed) and n > 0 and integ["完整"],
        "原因": reason,
        "完整": integ["完整"],
        "成功笔数": integ["成功笔数"],
        "缺口": integ["缺口"],
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
