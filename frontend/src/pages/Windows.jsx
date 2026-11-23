import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Windows() {
  const [page, setPage] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api.windowsPage().then(setPage).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const toggle = async (w) => {
    setErr('')
    try { await api.setWindowOpen(w.id, !w.开放); load() } catch (e) { setErr(e.message); load() }
  }
  if (!page && !err) return <p className="muted">加载窗口…</p>
  const wins = page?.窗口 || []
  const gate = page?.发号闸
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">柜面开闭</p>
        <h1>窗口</h1>
        <p className="lede">开闭柜面窗口，查看可办业务与当前在办号。</p>
      </header>
      {err && <p className="err">{err}</p>}
      {gate && (
        <section className="panel">
          <div className="stat-grid">
            <div className="stat"><span>开放窗口</span><strong>{gate.开放窗口}</strong></div>
            <div className="stat"><span>发号闸</span><strong style={{ fontSize: '1.2rem' }}>{gate.可发号 ? '可发号' : '已熔断'}</strong></div>
          </div>
          {!gate.可发号 && <p className="err">当前零开放窗口，取号将整笔拒发；号段游标不会推进。</p>}
          {gate.完整 === false && (
            <p className="err">
              发号证据缺口未清（{gate.缺口?.length || 0} 处）：成功灯须能与开放窗、游标、候队列、
              成功流水同命运重放。
            </p>
          )}
        </section>
      )}
      <div className="card-grid">
        {wins.map(w => (
          <div className="panel" key={w.id}>
            <h2>{w.名称} <span className="tag">{w.开放 ? '开放' : '关闭'}</span></h2>
            <p className="muted">可办：{w.可办种类.map(k => k.名称).join('、') || '无'}</p>
            {w.当前号 ? (
              <div className="slip">
                <div className="no">{w.当前号.号牌}</div>
                <div className="meta">{w.当前号.状态}</div>
              </div>
            ) : <p className="muted">空闲</p>}
            <div className="actions">
              <button type="button" className={w.开放 ? 'ghost' : ''} onClick={() => toggle(w)}>
                {w.开放 ? '关闭窗口' : '开放窗口'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
