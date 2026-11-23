"""假发号收口夹具：单号绿开放窗为零、游标加事件无行、墙空流水成功、收档对撞半截。

统一口径：成功灯必须能由 开放窗计数 ∪ 号段游标 ∪ 等候队列表 ∪ 成功流水行 同命运重放；
任一侧不成立即整笔回滚并回退流水成功行，禁止只改成功单号文案。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

# 必须在导入 app.* 之前指向临时库
_TMP = tempfile.mkdtemp(prefix="callslip-tear-")
os.environ["CALLSLIP_DB"] = str(Path(_TMP) / "test.db")

from app import db as dbmod  # noqa: E402
from app.db import db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import core, dayseal, reconcile, tickets, windows  # noqa: E402


def reset() -> None:
    if dbmod.DB_PATH.exists():
        dbmod.DB_PATH.unlink()
    seed_if_empty()


def kid(conn) -> int:
    return conn.execute("SELECT id FROM business_kinds ORDER BY id LIMIT 1").fetchone()["id"]


def snapshot(conn, kind_id: int) -> dict:
    day = core.business_day(conn)
    return {
        "seq": conn.execute(
            "SELECT seq FROM business_kinds WHERE id=?", (kind_id,)
        ).fetchone()["seq"],
        "tickets": conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE business_day=?", (day,)
        ).fetchone()["c"],
        "waiting": conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE business_day=? AND status='等候'", (day,)
        ).fetchone()["c"],
        "success": conn.execute(
            "SELECT COUNT(*) c FROM issue_events WHERE business_day=? AND outcome='成功'",
            (day,),
        ).fetchone()["c"],
        "events": conn.execute("SELECT COUNT(*) c FROM issue_events").fetchone()["c"],
    }


class SuccessLightMustReplay(unittest.TestCase):
    """夹具一：成功回执亮绿，则开放窗计数、游标、队列表、成功流水必须同帧成立。"""

    def setUp(self):
        reset()

    def test_green_receipt_replays_from_four_surfaces(self):
        with db() as conn:
            k = kid(conn)
            wall0 = tickets.waiting_wall(conn)
            ov0 = dayseal.overview(conn)
            base_wall = sum(b["等候数"] for b in wall0["墙面"])
            base_ov = ov0["等候"]
            t = tickets.take_ticket(conn, k)
            # 回执亮绿
            self.assertTrue(t["回执"]["可发号"])
            self.assertGreaterEqual(t["回执"]["开放窗口"], 1)
            # 号牌行仍在且可查到
            row = conn.execute(
                "SELECT * FROM tickets WHERE id=?", (t["id"],)
            ).fetchone()
            self.assertIsNotNone(row, "成功回执必须对应真实号牌行")
            self.assertEqual(row["slip_no"], t["号牌"])
            self.assertEqual(row["status"], "等候")
            # 游标落到回执值
            seq = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (k,)
            ).fetchone()["seq"]
            self.assertEqual(seq, t["回执"]["号段游标"])
            # 成功流水行与号牌同命运
            ev = conn.execute(
                "SELECT * FROM issue_events WHERE ticket_id=?", (t["id"],)
            ).fetchone()
            self.assertEqual(ev["outcome"], "成功")
            self.assertEqual(ev["slip_no"], t["号牌"])
            self.assertGreaterEqual(ev["open_windows"], 1)
            self.assertTrue(ev["open_window_names"], "成功流水须带开放窗名快照")
            # 四表重放对账无缺口
            integ = core.issue_integrity(conn)
            self.assertTrue(integ["完整"], integ["缺口"])
            # 等候墙与总览都恰好多算进这一笔
            wall = tickets.waiting_wall(conn)
            self.assertEqual(sum(b["等候数"] for b in wall["墙面"]), base_wall + 1)
            self.assertEqual(dayseal.overview(conn)["等候"], base_ov + 1)

    def test_success_light_never_outruns_open_window(self):
        """开放窗为零时的任何成功宣称都是假发号。"""
        with db() as conn:
            conn.execute("UPDATE windows SET open=0")
            k = kid(conn)
            before = snapshot(conn, k)
            with self.assertRaises(Exception) as ctx:
                tickets.take_ticket(conn, k)
            self.assertIn("无开放窗口", str(ctx.exception))
            after = snapshot(conn, k)
        self.assertEqual(before["seq"], after["seq"])
        self.assertEqual(before["tickets"], after["tickets"])
        self.assertEqual(before["success"], after["success"], "半笔也不得留下成功流水")

    def test_success_light_must_have_ticket_row(self):
        """游标加事件却无行：写侧自检拦截并整笔回滚。"""
        with db() as conn:
            k = kid(conn)
            before = snapshot(conn, k)

        orig = tickets.verify_issue_replay

        def drop_row(conn, **kw):
            # 模拟「落号后行丢了但游标/事件写了」的半截脏写
            conn.execute("DELETE FROM tickets WHERE id=?", (kw["ticket_id"],))
            return orig(conn, **kw)

        tickets.verify_issue_replay = drop_row
        try:
            with self.assertRaises(Exception) as ctx:
                with db() as conn:
                    tickets.take_ticket(conn, k)
            self.assertIn("号牌行缺失", str(ctx.exception))
        finally:
            tickets.verify_issue_replay = orig

        with db() as conn:
            after = snapshot(conn, k)
            integ = core.issue_integrity(conn)
        self.assertEqual(before["seq"], after["seq"], "不得留下游标推进")
        self.assertEqual(before["tickets"], after["tickets"])
        self.assertEqual(before["success"], after["success"], "成功流水行须随整笔回滚")
        self.assertEqual(before["events"], after["events"])
        self.assertTrue(integ["完整"], integ["缺口"])


class WallEmptyButAuditGreen(unittest.TestCase):
    """夹具三：墙空而流水成功 —— 必须被对账判为缺口，并冻结新的成功宣称。"""

    def setUp(self):
        reset()

    def _plant_fake_success(self):
        """手工制造「墙空流水成功」：成功流水行在，号牌行被抽走。"""
        with db() as conn:
            k = kid(conn)
            day = core.business_day(conn)
            t = tickets.take_ticket(conn, k)
            tid, slip = t["id"], t["号牌"]
            # 删行但留流水（旧旁路正是此形态）
            conn.execute("DELETE FROM tickets WHERE id=?", (tid,))
        with db() as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM tickets WHERE id=?", (tid,)).fetchone()
            )
            integ = core.issue_integrity(conn)
            self.assertFalse(integ["完整"], "墙空流水成功必须被判为缺口")
            types = " ".join(g["类型"] for g in integ["缺口"])
            self.assertIn("成功无号牌行", types)
            # 冻结新的成功宣称
            with self.assertRaises(Exception) as ctx:
                tickets.take_ticket(conn, k)
            self.assertIn("缺口未清", str(ctx.exception))
            gate = core.issue_gate(conn)
            self.assertFalse(gate["可发号"])
            self.assertEqual(gate["原因"], "发号证据缺口未清")
            # 缺口针不得让等候墙/总览亮已发号
            wall = tickets.waiting_wall(conn)
            self.assertFalse(wall["发号闸"]["可发号"])
            self.assertFalse(dayseal.overview(conn)["发号闸"]["可发号"])
            return k, slip

    def test_wall_empty_audit_green_is_detected_and_frozen(self):
        k, _ = self._plant_fake_success()
        with db() as conn:
            self.assertFalse(core.issue_gate(conn)["可发号"])
            fixed = reconcile.repair_legacy_surfaces(conn)
            self.assertEqual(fixed["回退无行成功"], 1, "墙空流水成功须回退成功行")
            integ = core.issue_integrity(conn)
            self.assertTrue(integ["完整"], integ["缺口"])
            gate = core.issue_gate(conn)
            self.assertTrue(gate["可发号"], "缺口清后须能重新宣称可发号")
            # 修复后发号能正常重放，不再一绿一僵
            t = tickets.take_ticket(conn, k)
            self.assertTrue(t["回执"]["可发号"])
            self.assertTrue(core.issue_integrity(conn)["完整"])

    def test_repair_legacy_bypass_end_to_end(self):
        """复刻旧旁路整链：面板键 + 删号牌 + 回退游标 + 成功注记 → 修复后四表可重放。"""
        with db() as conn:
            k = kid(conn)
            day = core.business_day(conn)
            t = tickets.take_ticket(conn, k)
            tid, slip, seq_after = t["id"], t["号牌"], t["回执"]["号段游标"]
            # 旧旁路：删行、回退游标、成功改注记、写伪造面板
            conn.execute("DELETE FROM tickets WHERE id=?", (tid,))
            conn.execute(
                "UPDATE business_kinds SET seq=? WHERE id=?", (seq_after - 1, k)
            )
            conn.execute(
                "UPDATE issue_events SET outcome='成功注记' WHERE ticket_id=?", (tid,)
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_last','{\"ok\":true,\"等候\":1}')"
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('take_surface:%d','{\"ok\":true}')" % tid
            )
            self.assertFalse(core.issue_integrity(conn)["完整"])
        with db() as conn:
            fixed = reconcile.repair_legacy_surfaces(conn)
            self.assertEqual(fixed["冲正成功注记"], 1)
            integ = core.issue_integrity(conn)
            self.assertTrue(integ["完整"], integ["缺口"])
            self.assertTrue(fixed["清除面板键"])
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM settings WHERE key='surface_last' OR key LIKE 'take_surface:%'"
                ).fetchone()
            )
            wall = tickets.waiting_wall(conn)
            # 号牌行已删，墙不得凭伪造面板显示这笔等候
            slips = [x["号牌"] for b in wall["墙面"] for x in b["队列"]]
            self.assertNotIn(slip, slips)
            self.assertTrue(core.issue_gate(conn)["可发号"])

    def test_repair_clears_legacy_success_annotation(self):
        """旧旁路「成功注记」必须冲正为失败，且伪造面板键被清除。"""
        with db() as conn:
            k = kid(conn)
            day = core.business_day(conn)
            conn.execute(
                """INSERT INTO issue_events(
                       business_day, kind_id, kind_name, outcome, slip_no, ticket_id,
                       open_windows, open_window_names, seq_before, seq_after, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (day, k, "个人储蓄", "成功注记", "储999", None, 3, "一号窗", 3, 4, "", core.now()),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_ok','1') ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            self.assertFalse(core.issue_integrity(conn)["完整"])
        with db() as conn:
            fixed = reconcile.repair_legacy_surfaces(conn)
            self.assertEqual(fixed["冲正成功注记"], 1)
            self.assertIn("surface_ok", fixed["清除面板键"])
            integ = core.issue_integrity(conn)
            self.assertTrue(integ["完整"], integ["缺口"])
            self.assertIsNone(
                conn.execute("SELECT 1 FROM settings WHERE key='surface_ok'").fetchone()
            )


class IssueRaceCloseHalfTear(unittest.TestCase):
    """夹具四：收档对撞半截 —— 并发收档与发号不得一绿一僵，不得墙未刷流水已绿。"""

    def setUp(self):
        reset()

    def test_race_close_never_green_without_window_or_audit(self):
        with db() as conn:
            conn.execute("UPDATE windows SET open=0")
            last = conn.execute("SELECT id FROM windows ORDER BY id LIMIT 1").fetchone()["id"]
            conn.execute("UPDATE windows SET open=1 WHERE id=?", (last,))
            k = core.parse_kind_ids(
                conn.execute("SELECT kind_ids FROM windows WHERE id=?", (last,)).fetchone()[
                    "kind_ids"
                ]
            )[0]

        results: list[tuple[str, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(9)

        def issuer():
            barrier.wait()
            for _ in range(5):
                with db() as conn:
                    try:
                        t = tickets.take_ticket(conn, k)
                        opens = core.open_window_count(conn)
                        with lock:
                            results.append(("ok", opens))
                    except Exception as e:  # noqa: BLE001
                        with lock:
                            results.append((f"err:{e}", -1))

        def closer():
            barrier.wait()
            with db() as conn:
                try:
                    windows.set_open(conn, last, False)
                except Exception:  # noqa: BLE001
                    pass

        threads = [threading.Thread(target=issuer) for _ in range(8)] + [
            threading.Thread(target=closer)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        with db() as conn:
            ok = [r for r in results if r[0] == "ok"]
            success = conn.execute(
                "SELECT COUNT(*) c FROM issue_events WHERE outcome='成功' AND kind_id=?", (k,)
            ).fetchone()["c"]
            tickets_n = conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE kind_id=? AND status='等候'", (k,)
            ).fetchone()["c"]
            integ = core.issue_integrity(conn)
        for _, opens in ok:
            self.assertGreater(opens, 0, "成功灯不得在零开放窗时亮")
        self.assertEqual(success, len(ok), "成功流水须与成功灯同数")
        self.assertGreaterEqual(tickets_n, len(ok), "成功灯须各有号牌行在队列表可重放")
        self.assertTrue(integ["完整"], integ["缺口"])
        self.assertNotEqual(len(ok), 0 and None, "不得整场僵死")

    def test_no_permanent_split_after_race(self):
        """并发过后不得长期一绿一僵：闸与四表重放必须收敛到同一结论。"""
        with db() as conn:
            k = kid(conn)
            threads = [
                threading.Thread(
                    target=lambda: _issue_once(k)
                )
                for _ in range(6)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            gate = core.issue_gate(conn)
            integ = core.issue_integrity(conn)
            wall = tickets.waiting_wall(conn)
            ov = dayseal.overview(conn)
            page = windows.windows_page(conn)
        self.assertTrue(integ["完整"], integ["缺口"])
        self.assertTrue(gate["可发号"])
        for g in (wall["发号闸"], ov["发号闸"], page["发号闸"]):
            self.assertEqual(g["可发号"], gate["可发号"])
            self.assertEqual(g["完整"], gate["完整"])
            self.assertEqual(g["开放窗口"], gate["开放窗口"])


def _issue_once(k):
    with db() as conn:
        try:
            tickets.take_ticket(conn, k)
        except Exception:  # noqa: BLE001
            pass


class IssueApiAndAuditShareFrame(unittest.TestCase):
    """发号 API、游标接口、等候墙页、开闭页、总览桶、审计必须同刷。"""

    def setUp(self):
        reset()

    def test_all_read_paths_agree_with_audit_after_issue(self):
        with db() as conn:
            k = kid(conn)
            tickets.take_ticket(conn, k)
            gate = core.issue_gate(conn)
            wall = tickets.waiting_wall(conn)
            page = windows.windows_page(conn)
            ov = dayseal.overview(conn)
            events = tickets.list_issue_events(conn)
        for obj, label in ((wall, "等候墙"), (page, "窗口页"), (ov, "总览")):
            self.assertEqual(obj["发号闸"]["开放窗口"], gate["开放窗口"], label)
            self.assertEqual(obj["发号闸"]["可发号"], gate["可发号"], label)
            self.assertEqual(obj["发号闸"]["完整"], gate["完整"], label)
        self.assertEqual(events[0]["结果"], "成功")
        self.assertEqual(events[0]["开放窗口"], gate["开放窗口"])
        self.assertTrue(events[0]["开放窗口名"])
        self.assertEqual(events[0]["游标后"], events[0]["游标前"] + 1)


if __name__ == "__main__":
    unittest.main()
