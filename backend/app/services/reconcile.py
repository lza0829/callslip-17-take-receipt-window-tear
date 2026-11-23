"""假发号收口：对账 + 一次性修复，让成功灯重新可由四表重放。

口径（全局唯一，架构一）：成功灯 = 开放窗计数 ∪ 号段游标 ∪ 等候队列表 ∪ 成功流水行，
四者必须同命运。任何一侧不成立，就整笔回退成功流水行——不是改成功单号文案。

旧版「成功面板」旁路在发号提交后另开事务：写 surface_* 设置、删号牌行、
回退号段游标、把成功流水改成「成功注记」。本模块把这类脏写收敛回单一口径：

1. 「成功注记」不再是成功宣示 → 冲正为「失败」；
2. 成功流水行找不到号牌行（墙空流水成功）→ 回退为「失败」，
   若号牌行还在但缺开放窗证据（发号时零窗）→ 号牌作废，成功流水回退为「失败」；
3. 号段游标与当日最大成功 seq_after 不符 → 按成功流水夹正（游标听流水与号牌行）；
4. 删除 surface_* / take_surface:* 设置键，总览不再叠加伪造等候与「可发号」。

修复幂等：可重复执行；冲正后的行不再是「成功」，不会被二次处理。
"""
from __future__ import annotations

import json

from app.services.core import issue_integrity


def repair_legacy_surfaces(conn) -> dict:
    """把旧旁路的脏成功收敛回「四表同命运」，返回修复清单。"""
    reversed_annotation = 0
    reversed_orphan = 0
    voided = 0
    cursor_fixed: list[dict] = []

    # 1) 旧旁路的「成功注记」：不是成功，冲正为失败。
    cur = conn.execute(
        """UPDATE issue_events
           SET outcome='失败', seq_after=seq_before,
               reason=CASE WHEN reason='' THEN '旧版成功面板旁路已冲正'
                           ELSE reason || '；旧版成功面板旁路已冲正' END
           WHERE outcome='成功注记'"""
    )
    reversed_annotation = cur.rowcount or 0

    # 2) 成功流水行找不到号牌行：成功灯无队列表可重放 → 回退成功行。
    orphans = conn.execute(
        """SELECT e.id FROM issue_events e
           LEFT JOIN tickets t ON t.id = e.ticket_id
           WHERE e.outcome='成功' AND (e.ticket_id IS NULL OR t.id IS NULL)"""
    ).fetchall()
    for r in orphans:
        conn.execute(
            """UPDATE issue_events
               SET outcome='失败', seq_after=seq_before,
                   reason=CASE WHEN reason='' THEN '成功流水无号牌行，已回退'
                               ELSE reason || '；成功流水无号牌行，已回退' END
               WHERE id=?""",
            (r["id"],),
        )
    reversed_orphan = len(orphans)

    # 3) 成功行有号牌，但发号时零开放窗（缺开放窗证据）：整笔不成立。
    #    号牌已入队/已办理的保留历史但作废，使其不再在等候墙冒充已发号。
    bad_evidence = conn.execute(
        """SELECT e.id AS ev_id, e.ticket_id, t.status
           FROM issue_events e JOIN tickets t ON t.id = e.ticket_id
           WHERE e.outcome='成功' AND e.open_windows < 1"""
    ).fetchall()
    for r in bad_evidence:
        conn.execute(
            """UPDATE issue_events
               SET outcome='失败', seq_after=seq_before,
                   reason=CASE WHEN reason='' THEN '成功流水缺开放窗证据，已回退'
                               ELSE reason || '；成功流水缺开放窗证据，已回退' END
               WHERE id=?""",
            (r["ev_id"],),
        )
        if r["status"] in ("等候", "过号"):
            conn.execute(
                "UPDATE tickets SET status='已作废', note='发号缺开放窗证据，已作废' WHERE id=?",
                (r["ticket_id"],),
            )
            voided += 1

    # 4) 号段游标与当日成功流水不符：按最大成功 seq_after 夹正。
    #    只在当日确有成功能重放时改游标；当日无成功则不动（如切日刚重置）。
    for k in conn.execute("SELECT id, name, seq FROM business_kinds ORDER BY id").fetchall():
        exp = conn.execute(
            """SELECT MAX(seq_after) AS m FROM issue_events
               WHERE outcome='成功' AND kind_id=?
                 AND business_day=(SELECT value FROM settings WHERE key='营业日')""",
            (k["id"],),
        ).fetchone()["m"]
        if exp is not None and int(k["seq"]) != int(exp):
            conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (int(exp), k["id"]))
            cursor_fixed.append(
                {"业务种类": k["name"], "原游标": int(k["seq"]), "新游标": int(exp)}
            )

    # 5) 删除伪造面板键与残留快照。
    keys = [
        r["key"]
        for r in conn.execute(
            "SELECT key FROM settings WHERE key='surface_ok' OR key='surface_last' "
            "OR key LIKE 'take_surface:%'"
        ).fetchall()
    ]
    for key in keys:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    for r in conn.execute("SELECT id, note FROM tickets WHERE note LIKE '%surface%'").fetchall():
        try:
            json.loads(r["note"])
        except (TypeError, ValueError):
            continue
        conn.execute("UPDATE tickets SET note='' WHERE id=?", (r["id"],))

    return {
        "冲正成功注记": reversed_annotation,
        "回退无行成功": reversed_orphan,
        "作废缺窗号牌": voided,
        "夹正游标": cursor_fixed,
        "清除面板键": sorted(keys),
        "修复后对账": issue_integrity(conn),
    }


def reconcile(conn) -> dict:
    """对账只读视图：成功灯能否由开放窗∪游标∪队列表∪成功流水重放。"""
    return issue_integrity(conn)
