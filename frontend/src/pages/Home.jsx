import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => api.overview().then(setO).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const repair = async () => {
    setErr(''); setMsg('')
    try {
      const r = await api.repairIssue()
      const f = r.修复 || {}
      setMsg(
        `已收口：冲正成功注记 ${f.冲正成功注记 || 0} 笔 · 回退无行成功 ${f.回退无行成功 || 0} 笔 · ` +
        `夹正游标 ${(f.夹正游标 || []).length} 项 · 清除面板键 ${(f.清除面板键 || []).length} 个。` +
        `修复后对账：${r.对账?.完整 ? '四表可重放' : '仍有缺口'}。`
      )
      load()
    } catch (e) { setErr(e.message) }
  }
  if (err) return <p className="err">{err}</p>
  if (!o) return <p className="muted">加载总览…</p>
  const cards = [
    ['等候', o.等候], ['办理中', o.办理中], ['过号', o.过号],
    ['已办结', o.已办结], ['已作废', o.已作废], ['开放窗口', o.开放窗口],
  ]
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">网点队列物化</p>
        <h1>叫号条</h1>
        <p className="lede">
          营业日 {o.营业日} · {o.已封口 ? '已封口' : '营业中'}。取号入队，窗口叫号推进，过号可回收重叫，当日封口后禁写。
        </p>
      </header>
      <div className="stat-grid">
        {cards.map(([k, v]) => (
          <div className="stat" key={k}><span>{k}</span><strong>{v}</strong></div>
        ))}
      </div>
      {o.发号闸 && (
        <section className="panel">
          <h2>发号闸</h2>
          <p className={o.发号闸.可发号 ? 'muted' : 'err'}>
            {o.发号闸.可发号
              ? `可发号 · 开放窗口 ${o.发号闸.开放窗口}`
              : `已熔断 · ${o.发号闸.原因} · 号牌不发、游标不推进`}
          </p>
          {o.发号闸.完整 === false ? (
            <p className="err">
              发号证据对不上账：{o.发号闸.缺口.length} 处缺口。
              成功灯须能由开放窗 ∪ 号段游标 ∪ 队列表 ∪ 成功流水重放，否则不得宣称已发号。
            </p>
          ) : (
            <p className="muted">成功灯与开放窗、号段游标、候队列、成功流水已对账一致。</p>
          )}
          {o.发号闸.完整 === false && (
            <div className="actions">
              <button type="button" onClick={repair}>对账收口</button>
            </div>
          )}
          {msg && <p className="muted">{msg}</p>}
        </section>
      )}
      <section className="panel">
        <h2>窗口现况</h2>
        <ul className="plain">
          {o.窗口.map(w => (
            <li key={w.id}>
              {w.名称} · {w.开放 ? '开放' : '关闭'} · {w.有在办 ? '有在办号' : '空闲'}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
