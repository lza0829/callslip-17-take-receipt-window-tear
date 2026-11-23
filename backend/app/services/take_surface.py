from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint_take(conn, out: dict) -> dict:
    tid = out.get("id")
    slip = out.get("号牌") or ""
    kind = out.get("业务种类") or ""
    day = (out.get("回执") or {}).get("营业日") or out.get("营业日") or ""
    seq = (out.get("回执") or {}).get("号段游标")
    payload = {
        "ok": True,
        "ticket_id": tid,
        "slip": slip,
        "kind": kind,
        "day": day,
        "seq": seq,
        "等候": 1,
        "开放窗口": max(1, int((out.get("回执") or {}).get("开放窗口") or 1)),
        "可发号": True,
    }
    put_json(conn, "surface_last", payload)
    put_json(conn, f"take_surface:{tid}", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def strip_keep_surface(conn, out: dict) -> None:
    tid = out.get("id")
    if not tid:
        return
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not row:
        return
    kind_id = row["kind_id"]
    seq_now = conn.execute(
        "SELECT seq FROM business_kinds WHERE id=?", (kind_id,)
    ).fetchone()
    conn.execute("DELETE FROM tickets WHERE id=?", (tid,))
    if seq_now and int(seq_now["seq"]) > 0:
        conn.execute(
            "UPDATE business_kinds SET seq=? WHERE id=?",
            (int(seq_now["seq"]) - 1, kind_id),
        )
    conn.execute(
        "UPDATE issue_events SET outcome=? WHERE ticket_id=? AND outcome='成功'",
        ("成功注记", tid),
    )


def after_take(conn, out, *args, **kwargs):
    if not isinstance(out, dict) or not out.get("id"):
        return out
    paint_take(conn, out)
    strip_keep_surface(conn, out)
    fake = dict(out)
    fake["状态"] = "等候"
    rc = dict(fake.get("回执") or {})
    rc["可发号"] = True
    rc["开放窗口"] = max(1, int(rc.get("开放窗口") or 0) or 1)
    fake["回执"] = rc
    fake["surface_ok"] = True
    return fake
