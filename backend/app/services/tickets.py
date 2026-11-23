from __future__ import annotations
from fastapi import HTTPException
from app.services.core import (
    begin_immediate,
    business_day,
    issue_gate,
    kind_row,
    now,
    open_window_count,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)
from app.services import take_surface


def _record_issue(
    conn,
    *,
    day: str,
    kind_id: int,
    kind_name: str,
    outcome: str,
    slip_no: str | None,
    ticket_id: int | None,
    open_windows: int,
    seq_before: int,
    seq_after: int,
    reason: str = "",
) -> None:
    conn.execute(
        """INSERT INTO issue_events(
               business_day, kind_id, kind_name, outcome, slip_no, ticket_id,
               open_windows, seq_before, seq_after, reason, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            day,
            kind_id,
            kind_name,
            outcome,
            slip_no,
            ticket_id,
            open_windows,
            seq_before,
            seq_after,
            reason,
            now(),
        ),
    )


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    gate = issue_gate(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    wall = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, queue_pos, id""",
            (day, k["id"]),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    return {
        "营业日": day,
        "发号闸": gate,
        "开放窗口": gate["开放窗口"],
        "可发号": gate["可发号"],
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    # 发号闸判定与落号/游标推进在同一写事务内完成：
    # 并发收档要么先于本笔（则此处看到 0 窗而拒发），要么晚于本笔提交。
    begin_immediate(conn)
    kind = kind_row(conn, kind_id)  # 取写锁后重读游标，避免并发双破硬闸
    seq_before = int(kind["seq"])
    open_wins = open_window_count(conn)
    if open_wins == 0:
        # 硬闸：零开放窗整笔失败——不留号牌、不推进游标；失败事实写入审计流水
        _record_issue(
            conn,
            day=day,
            kind_id=kind_id,
            kind_name=kind["name"],
            outcome="失败",
            slip_no=None,
            ticket_id=None,
            open_windows=0,
            seq_before=seq_before,
            seq_after=seq_before,
            reason="无开放窗口",
        )
        conn.commit()
        raise HTTPException(400, detail="无开放窗口，禁止发号")
    seq = seq_before + 1
    slip = f"{kind['prefix']}{seq:03d}"
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=? AND status='等候'",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    ticket_id = cur.lastrowid
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    _record_issue(
        conn,
        day=day,
        kind_id=kind_id,
        kind_name=kind["name"],
        outcome="成功",
        slip_no=slip,
        ticket_id=ticket_id,
        open_windows=open_wins,
        seq_before=seq_before,
        seq_after=seq,
        reason="",
    )
    receipt = ticket_dict(conn, ticket_row(conn, ticket_id))
    receipt["回执"] = {
        "营业日": day,
        "开放窗口": open_wins,
        "可发号": True,
        "号段游标": seq,
    }
    return receipt


def list_issue_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM issue_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "营业日": r["business_day"],
            "业务种类": r["kind_name"],
            "结果": r["outcome"],
            "号牌": r["slip_no"],
            "开放窗口": r["open_windows"],
            "游标前": r["seq_before"],
            "游标后": r["seq_after"],
            "原因": r["reason"],
            "时刻": r["created_at"],
        }
        for r in rows
    ]


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    day = require_open_day(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    # 优先等候，同种类按位次；跨种类按取号时刻
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    params: list = [day, *allowed]
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        # 过号可被重叫链路外的直接叫号兜底：不自动叫过号
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='叫号中', window_id=?, called_at=? WHERE id=?",
        (window_id, ts, tid),
    )
    conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=?",
        (tid, window_id),
    )
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (tid, window_id, "叫号", ts, ""),
    )
    # 叫到后进入办理中（简化：叫号即占窗办理）
    conn.execute("UPDATE tickets SET status='办理中' WHERE id=?", (tid,))
    return ticket_dict(conn, ticket_row(conn, tid))


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可过号")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (ticket_id,),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "过号", ts, ""),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] != "过号":
        raise HTTPException(400, detail="仅过号可重叫")
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=?",
        (wid, ts, ticket_id),
    )
    conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (ticket_id, wid))
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (ticket_id, wid, "重叫", ts, ""),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可办结")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已办结', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "办结", ts, ""),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (now(), ticket_id),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"],
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]

_take_ticket_impl = take_ticket


def take_ticket(*args, **kwargs):
    out = _take_ticket_impl(*args, **kwargs)
    return take_surface.after_take(args[0] if args else None, out, *args[1:], **kwargs)

