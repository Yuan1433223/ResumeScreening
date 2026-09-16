import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Scheduling } from '../types'

const BOOK: Record<string, { t: string; c: string }> = {
  pending: { t: '待确认', c: 'muted' },
  confirmed: { t: '已确认', c: 'ok' },
  waitlist: { t: '候补', c: 'warn' },
  cancelled: { t: '已取消', c: 'bad' },
}

const NOTIFY: Record<string, { t: string; c: string }> = {
  succeeded: { t: '已通知', c: 'ok' },
  failed: { t: '通知失败', c: 'bad' },
  pending: { t: '待通知', c: 'warn' },
  '': { t: '未发起', c: 'muted' },
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export default function SchedulingPage() {
  const [data, setData] = useState<Scheduling | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)

  const load = () => {
    api
      .scheduling()
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    load()
  }, [])

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      setData(await api.runScheduling())
    } catch (e) {
      const msg = (e as Error).message
      setError(msg)
      if (msg.includes('已排期')) load()
    } finally {
      setRunning(false)
    }
  }

  return (
    <div>
      <h1 className="page-title">排期</h1>
      <p className="page-sub">审批通过名单统一排序、分配时段；模拟建会与通知（幂等，可重复查询）。</p>

      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <button className="btn primary" onClick={run} disabled={running}>
          {running ? '排期中…' : '生成排期'}
        </button>
        <button className="btn" onClick={load}>
          刷新
        </button>
        {data?.round && (
          <span className="hint">
            批次 {data.batch_id} · 第 {data.round.round_no} 轮 · 状态 {data.round.status}
          </span>
        )}
      </div>

      <div className="card">
        <div className="card-title">时段</div>
        {!data?.slots.length && <div className="hint">尚无排期。先创建批次并审批候选人，再生成排期。</div>}
        {data && data.slots.length > 0 && (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>开始</th>
                  <th>结束</th>
                  <th>状态</th>
                  <th>候选人</th>
                </tr>
              </thead>
              <tbody>
                {data.slots.map((s) => (
                  <tr key={s.slot_id}>
                    <td className="num">{fmtTime(s.start)}</td>
                    <td className="num">{fmtTime(s.end)}</td>
                    <td>
                      {s.status === 'taken' ? (
                        <span className="status">
                          <span className="dot ok" />
                          已占用
                        </span>
                      ) : (
                        <span className="status">
                          <span className="dot muted" />
                          空闲
                        </span>
                      )}
                    </td>
                    <td>{s.candidate ? <a className="link" href={`#/review/${encodeURIComponent(s.document_id!)}`}>{s.candidate}</a> : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">预约与通知</div>
        {!data?.bookings.length && <div className="hint">暂无预约记录。</div>}
        {data && data.bookings.length > 0 && (
          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>候选人</th>
                  <th>时段</th>
                  <th>预约状态</th>
                  <th>通知</th>
                </tr>
              </thead>
              <tbody>
                {data.bookings.map((b) => {
                  const bk = BOOK[b.status] ?? { t: b.status, c: 'muted' }
                  const nt = NOTIFY[b.notify_status] ?? { t: b.notify_status, c: 'muted' }
                  const slot = data.slots.find((s) => s.slot_id === b.slot_id)
                  return (
                    <tr key={b.booking_id}>
                      <td>
                        <a className="link" href={`#/review/${encodeURIComponent(b.document_id)}`}>
                          {b.candidate}
                        </a>
                      </td>
                      <td className="num">{slot ? fmtTime(slot.start) : '—'}</td>
                      <td>
                        <span className="status">
                          <span className={`dot ${bk.c}`} />
                          {bk.t}
                        </span>
                      </td>
                      <td>
                        <span className="status">
                          <span className={`dot ${nt.c}`} />
                          {nt.t}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
