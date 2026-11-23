# 叫号条（Callslip）

银行网点柜面叫号：取号入队 → 叫号推进 → 过号回收/重叫 → 当日队列封口。

- 前端 http://127.0.0.1:6400
- API http://127.0.0.1:11400
- `docker compose up -d --build`

## 无开放窗发号闸（架构一：开放窗硬闸）

全局只贯彻一种口径：**开放窗口为零时整笔发号失败**——不留号牌、不推进号段游标，
失败事实写入发号审计流水。任何读路径都不得把失败发号算进等候。

- 开放窗口唯一谓词：`windows.open=1`，定义在 `app/services/core.py` 的
  `OPEN_WINDOW_PREDICATE` / `open_window_count()`；发号写入侧、等候墙、总览、
  窗口开闭页、发号回执全部经 `issue_gate()` 取同一口径。
- 发号事务：`take_ticket` 以 `BEGIN IMMEDIATE` 取写锁后重读游标，在同一事务内
  完成「闸判定 → 落号 → 推游标 → 写审计」；任一步异常整笔回滚到发号前快照。
  收档（`set_open(..., False)`）同样先取写锁，与发号串行，杜绝并发双破硬闸。
- 同刷接口：`GET /api/发号闸`、`/api/等候墙`、`/api/总览`、`/api/窗口页` 均带
  `发号闸`（`可发号`/`开放窗口`/`原因`）；`POST /api/取号` 成功回执带 `回执`，
  失败返回 400 并落 `发号流水`（`GET /api/发号流水`）。
- 测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`
  覆盖零窗拒发、失败不入队、游标不动、并发发号不跳号、并发收档抢最后窗口、
  半截失败回滚。

