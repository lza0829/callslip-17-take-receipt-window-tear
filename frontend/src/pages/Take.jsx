import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Take() {
  const [kinds, setKinds] = useState([])
  const [kid, setKid] = useState('')
  const [slip, setSlip] = useState(null)
  const [gate, setGate] = useState(null)
  const [err, setErr] = useState('')

  const load = async () => {
    try {
      const [ks, g] = await Promise.all([api.kinds(), api.issueGate()])
      setKinds(ks.filter(k => k.启用))
      setGate(g)
      setKid(prev => prev || String(ks.find(k => k.启用)?.id || ''))
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])

  const onTake = async () => {
    setErr('')
    try {
      const t = await api.take(Number(kid))
      setSlip(t)
      load()
    } catch (e) {
      setSlip(null)
      setErr(e.message)
      load()
    }
  }

  const blocked = gate && !gate.可发号
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">发号入队</p>
        <h1>取号</h1>
        <p className="lede">按业务种类发放当日号牌，写入等候队列；零开放窗口时整笔拒发。</p>
      </header>
      {gate && (
        <section className="panel">
          <div className="stat-grid">
            <div className="stat"><span>营业日</span><strong style={{ fontSize: '1.2rem' }}>{gate.营业日}</strong></div>
            <div className="stat"><span>开放窗口</span><strong>{gate.开放窗口}</strong></div>
            <div className="stat"><span>发号闸</span><strong style={{ fontSize: '1.2rem' }}>{gate.可发号 ? '可发号' : '已熔断'}</strong></div>
          </div>
          {!gate.可发号 && (
            <p className="err">发号已熔断：{gate.原因}。号牌不会发放，号段游标不会推进。</p>
          )}
          {gate.完整 === false && (
            <p className="err">
              发号证据缺口未清（{gate.缺口?.length || 0} 处）。成功灯须能由开放窗 ∪ 号段游标 ∪
              候队列 ∪ 成功流水重放，缺口未清不得宣称已发号。
            </p>
          )}
        </section>
      )}
      <section className="panel">
        <div className="form row">
          <label>业务种类
            <select value={kid} onChange={e => setKid(e.target.value)}>
              {kinds.map(k => <option key={k.id} value={k.id}>{k.名称}（{k.前缀}）</option>)}
            </select>
          </label>
          <button type="button" onClick={onTake} disabled={!kid || blocked}>取号</button>
        </div>
        {err && <p className="err">{err}</p>}
        {slip && (
          <div className="slip" style={{ marginTop: 16 }}>
            <div className="no">{slip.号牌}</div>
            <div className="meta">{slip.业务种类} · {slip.状态} · 位次 {slip.位次}</div>
            {slip.回执 && (
              <div className="meta">
                回执 · 开放窗口 {slip.回执.开放窗口} · 号段游标 {slip.回执.号段游标}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
