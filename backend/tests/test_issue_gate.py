"""无开放窗发号闸的对照测试：零窗发号、并发发号、并发收档、半截失败。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

# 必须在导入 app.* 之前指向临时库
_TMP = tempfile.mkdtemp(prefix="callslip-test-")
os.environ["CALLSLIP_DB"] = str(Path(_TMP) / "test.db")

from app import db as dbmod  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import core, tickets, windows  # noqa: E402


def reset() -> None:
    if dbmod.DB_PATH.exists():
        dbmod.DB_PATH.unlink()
    seed_if_empty()


def set_all_windows(open_: bool) -> None:
    with db() as conn:
        conn.execute("UPDATE windows SET open=?", (1 if open_ else 0,))


def kind_id(conn) -> int:
    return conn.execute("SELECT id FROM business_kinds ORDER BY id LIMIT 1").fetchone()["id"]


class ZeroWindowGate(unittest.TestCase):
    def setUp(self):
        reset()

    def test_no_open_window_blocks_issue_and_keeps_cursor(self):
        set_all_windows(False)
        with db() as conn:
            kid = kind_id(conn)
            before = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
            n_before = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
            with self.assertRaises(Exception) as ctx:
                tickets.take_ticket(conn, kid)
            self.assertIn("无开放窗口", str(ctx.exception))
            after = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
            n_after = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        self.assertEqual(before, after, "失败发号不得推进游标")
        self.assertEqual(n_before, n_after, "失败发号不得留号牌")

    def test_failed_issue_is_audited(self):
        set_all_windows(False)
        with db() as conn:
            kid = kind_id(conn)
            with self.assertRaises(Exception):
                tickets.take_ticket(conn, kid)
            ev = tickets.list_issue_events(conn)
        self.assertTrue(ev)
        self.assertEqual(ev[0]["结果"], "失败")
        self.assertEqual(ev[0]["开放窗口"], 0)
        self.assertEqual(ev[0]["游标前"], ev[0]["游标后"])

    def test_failed_issue_not_in_waiting_wall_or_overview(self):
        set_all_windows(False)
        with db() as conn:
            kid = kind_id(conn)
            wall0 = tickets.waiting_wall(conn)
            waiting0 = sum(b["等候数"] for b in wall0["墙面"])
            with self.assertRaises(Exception):
                tickets.take_ticket(conn, kid)
            wall1 = tickets.waiting_wall(conn)
            ov = __import__("app.services.dayseal", fromlist=["overview"]).overview(conn)
        self.assertEqual(waiting0, sum(b["等候数"] for b in wall1["墙面"]))
        self.assertEqual(ov["等候"], waiting0)
        self.assertEqual(ov["发号闸"]["开放窗口"], 0)
        self.assertFalse(ov["发号闸"]["可发号"])

    def test_receipt_reflects_gate_and_cursor_advances(self):
        with db() as conn:
            kid = kind_id(conn)
            seq0 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
            t = tickets.take_ticket(conn, kid)
            seq1 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
            self.assertEqual(t["回执"]["可发号"], True)
            self.assertEqual(t["回执"]["号段游标"], seq0 + 1)
            self.assertEqual(seq1, seq0 + 1)

    def test_all_read_paths_share_gate(self):
        set_all_windows(False)
        with db() as conn:
            gate = core.issue_gate(conn)
            wall = tickets.waiting_wall(conn)
            page = windows.windows_page(conn)
            ov = __import__("app.services.dayseal", fromlist=["overview"]).overview(conn)
        for g in (wall["发号闸"], page["发号闸"], ov["发号闸"]):
            self.assertEqual(g["开放窗口"], 0)
            self.assertFalse(g["可发号"])
        self.assertEqual(gate["开放窗口"], 0)


class Concurrency(unittest.TestCase):
    def setUp(self):
        reset()

    def test_concurrent_issues_do_not_duplicate_or_skip_cursor(self):
        with db() as conn:
            kid = kind_id(conn)
            seq0 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]

        outcomes: list[str] = []
        lock = threading.Lock()

        def worker():
            with db() as conn:
                try:
                    t = tickets.take_ticket(conn, kid)
                    with lock:
                        outcomes.append(t["号牌"])
                except Exception as e:  # noqa: BLE001
                    with lock:
                        outcomes.append(f"ERR:{e}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        with db() as conn:
            seq1 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
        ok = [o for o in outcomes if not o.startswith("ERR")]
        self.assertEqual(len(ok), 8, outcomes)
        self.assertEqual(len(set(ok)), 8, "号牌不得重复")
        self.assertEqual(seq1, seq0 + 8, "游标推进须与成功发号笔数一致")

    def test_issue_racing_close_last_window_never_leaves_ticket_without_window(self):
        """并发收档抢最后开放窗口：所有成功发号的瞬间都必须至少有一个开放窗，
        且收档成功之后不得再有任何成功发号。"""
        with db() as conn:
            conn.execute("UPDATE windows SET open=0")
            last = conn.execute("SELECT id FROM windows ORDER BY id LIMIT 1").fetchone()["id"]
            conn.execute("UPDATE windows SET open=1 WHERE id=?", (last,))
            allowed = core.parse_kind_ids(
                conn.execute(
                    "SELECT kind_ids FROM windows WHERE id=?", (last,)
                ).fetchone()["kind_ids"]
            )
            kid = allowed[0]

        results: list[tuple[str, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(9)
        close_mark = {"audit_id": None}

        def issuer():
            barrier.wait()
            for _ in range(6):
                with db() as conn:
                    try:
                        t = tickets.take_ticket(conn, kid)
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
                    mark = conn.execute(
                        "SELECT COALESCE(MAX(id),0) m FROM issue_events"
                    ).fetchone()["m"]
                    with lock:
                        close_mark["audit_id"] = mark
                except Exception:  # noqa: BLE001
                    pass

        threads = [threading.Thread(target=issuer) for _ in range(8)] + [
            threading.Thread(target=closer)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        ok = [r for r in results if r[0] == "ok"]
        for _, opens in ok:
            self.assertGreater(opens, 0, "成功发号时不得为 0 开放窗")

        with db() as conn:
            open_now = core.open_window_count(conn)
            success_audits = conn.execute(
                "SELECT COUNT(*) c FROM issue_events WHERE outcome='成功' AND kind_id=?",
                (kid,),
            ).fetchone()["c"]
            if close_mark["audit_id"] is not None:
                late = conn.execute(
                    """SELECT COUNT(*) c FROM issue_events
                       WHERE outcome='成功' AND kind_id=? AND id > ?""",
                    (kid, close_mark["audit_id"]),
                ).fetchone()["c"]
        self.assertEqual(success_audits, len(ok), "成功审计笔数须等于成功发号笔数")
        if open_now == 0 and close_mark["audit_id"] is not None:
            self.assertEqual(late, 0, "收档成功后不得再发号")


class HalfFailure(unittest.TestCase):
    """半截失败脏写：落号后抛错须整笔回滚到发号前快照。"""

    def setUp(self):
        reset()

    def test_failure_after_insert_rolls_back_ticket_and_cursor(self):
        with db() as conn:
            kid = kind_id(conn)
            snap = {
                "seq": conn.execute(
                    "SELECT seq FROM business_kinds WHERE id=?", (kid,)
                ).fetchone()["seq"],
                "tickets": conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"],
                "waiting": conn.execute(
                    "SELECT COUNT(*) c FROM tickets WHERE status='等候'"
                ).fetchone()["c"],
                "audit": conn.execute("SELECT COUNT(*) c FROM issue_events").fetchone()["c"],
            }

        orig_record = tickets._record_issue
        calls = {"n": 0}

        def boom(conn, **kw):
            calls["n"] += 1
            if kw.get("outcome") == "成功":
                # 模拟落号+推游标之后的半截失败
                raise RuntimeError("模拟写审计失败")
            return orig_record(conn, **kw)

        tickets._record_issue = boom
        try:
            # 异常必须穿出 with db() 才会触发回滚（路由层亦如此）
            with self.assertRaises(RuntimeError):
                with db() as conn:
                    tickets.take_ticket(conn, kid)
        finally:
            tickets._record_issue = orig_record

        with db() as conn:
            self.assertEqual(
                conn.execute("SELECT seq FROM business_kinds WHERE id=?", (kid,)).fetchone()["seq"],
                snap["seq"],
                "半截失败须回滚游标",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"],
                snap["tickets"],
                "半截失败须回滚号牌",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='等候'").fetchone()["c"],
                snap["waiting"],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM issue_events").fetchone()["c"],
                snap["audit"],
                "半截失败不得留下残缺审计",
            )

    def test_retry_after_half_failure_succeeds_cleanly(self):
        with db() as conn:
            kid = kind_id(conn)
            seq0 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
        orig_record = tickets._record_issue
        tickets._record_issue = lambda conn, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                with db() as conn:
                    tickets.take_ticket(conn, kid)
        finally:
            tickets._record_issue = orig_record
        with db() as conn:
            t = tickets.take_ticket(conn, kid)
            seq1 = conn.execute(
                "SELECT seq FROM business_kinds WHERE id=?", (kid,)
            ).fetchone()["seq"]
        slip_num = int(t["号牌"][len(t["前缀"]):])
        self.assertEqual(seq1, seq0 + 1)
        self.assertEqual(slip_num, seq0 + 1, "重试须用连续号段，不得跳号")


class WindowReopen(unittest.TestCase):
    def setUp(self):
        reset()

    def test_reopen_restores_issue(self):
        set_all_windows(False)
        with db() as conn:
            kid = kind_id(conn)
            with self.assertRaises(Exception):
                tickets.take_ticket(conn, kid)
        with db() as conn:
            wid = conn.execute("SELECT id FROM windows ORDER BY id LIMIT 1").fetchone()["id"]
            windows.set_open(conn, wid, True)
            t = tickets.take_ticket(conn, kid)
            self.assertEqual(t["回执"]["开放窗口"], 1)


if __name__ == "__main__":
    unittest.main()
