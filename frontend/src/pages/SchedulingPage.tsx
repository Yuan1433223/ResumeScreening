import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'
import type { Scheduling } from '../types'
const names: Record<string, string> = { pending: '等待处理', confirmed: '已确认', waitlist: '候补', cancelled: '已取消', succeeded: '已完成', failed: '失败，待重试', '': '未发起' }
const time = (iso: string) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(iso))
export default function SchedulingPage() {
  const [data, setData] = useState<Scheduling | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  const batchId = window.localStorage.getItem('batch_id') || ''
  const load = () => { setError(''); api.scheduling(batchId).then(setData).catch((e: Error) => setError(e.message)) }
  useEffect(load, [batchId])
  const run = async () => { setRunning(true); setError(''); setMessage(''); try { const next = await api.runScheduling(batchId); setData(next); setMessage(next.bookings.some(b => b.meeting_status === 'failed' || b.notify_status === 'failed') ? '部分动作尚未完成，可重试失败动作。' : next.bookings.length ? '处理完成，已有成功动作与预约记录已保留。' : '本批次没有可排期的已批准候选人。') } catch (e) { setError((e as Error).message) } finally { setRunning(false) } }
  const hasFailures = data?.bookings.some(b => b.meeting_status === 'failed' || b.notify_status === 'failed')
  return <div>
    <div className="page-heading"><div><div className="eyebrow">人工批准后的下一步</div><h1 className="page-title">排期与重试</h1><p className="page-sub">保留已完成的预约与会议，失败时只补未完成的动作。</p></div><span className="badge neutral">模拟会议与通知</span></div>
    {error && <div className="notice danger" role="alert">{error}</div>}{message && <div className={`notice ${hasFailures ? 'warning' : 'positive'}`} role="status">{message}</div>}
    <div className="card scheduling-intro"><span className="principle-icon"><Icon name="retry" size={24} /></span><div><h2>一次预约，只保留一份记录</h2><p>会议已创建、通知失败时，重试沿用原会议。已经成功的动作不会重复执行。</p></div><div className="toolbar"><button className="btn" onClick={load} disabled={running}>刷新状态</button><button className="btn primary" onClick={run} disabled={running || !data?.batch_id || (!data.round && !data.approved_count)}><Icon name={data?.round ? 'retry' : 'calendar'} size={16} />{running ? '正在处理…' : data?.round ? hasFailures ? '重试失败动作' : '检查并继续处理' : '结束审批并生成排期'}</button></div></div>
    {!data?.round && Boolean(data?.approved_count) && <div className="notice warning">生成排期会结束本批次审批。当前已批准 {data?.approved_count} 人，仍有 {data?.pending_count || 0} 人待处理；请先确认本轮名单，未处理项需在新批次继续。</div>}
    <div className="section-heading schedule-section"><div><h2>面试时段</h2><p className="hint">北京时间 · 每个时段 45 分钟 · 按本批次人工批准名单分配</p></div><span className="badge neutral">{data?.slots.filter(s => s.status === 'taken').length || 0} 个已安排</span></div>
    {data?.slots.length ? <div className="slot-grid">{data.slots.map((slot, i) => <div className={`slot-card ${slot.status === 'taken' ? 'occupied' : ''}`} key={slot.slot_id}><span className="slot-index">时段 {String(i + 1).padStart(2, '0')}</span><span className={`badge ${slot.status === 'taken' ? 'positive' : 'neutral'}`}>{slot.status === 'taken' ? '已安排' : '可用'}</span><h3>{time(slot.start)}</h3><p>至 {time(slot.end).split(' ').pop()}</p><div className="slot-candidate">{slot.candidate ? <a href={`#/review/${encodeURIComponent(slot.document_id!)}`}><Icon name="check" size={15} />{slot.candidate}</a> : '等待候选人安排'}</div></div>)}</div> : <div className="card empty-state"><Icon name="calendar" size={30} /><h3>尚未生成排期</h3><p>先在批次中核对证据并批准候选人，再在这里安排时段。</p><a className="btn" href="#/batches">返回批次与复核</a></div>}
    <section className="results-panel"><div className="results-toolbar"><div><h2>预约与执行状态</h2><span className="hint">创建会议成功后才发送通知</span></div><span className="count">{data?.bookings.length || 0}</span></div>{data?.bookings.length ? <div className="table-wrap"><table className="grid"><thead><tr><th>候选人</th><th>预约</th><th>模拟会议</th><th>模拟通知</th><th>处理记录</th></tr></thead><tbody>{data.bookings.map(booking => <tr key={booking.booking_id}><td><a className="link" href={`#/review/${encodeURIComponent(booking.document_id)}`}>{booking.candidate}</a></td><td><span className={`badge ${booking.status === 'confirmed' ? 'positive' : 'warning'}`}>{names[booking.status] || booking.status}</span></td><td><span className={`dot ${booking.meeting_status === 'succeeded' ? 'ok' : booking.meeting_status === 'failed' ? 'bad' : 'muted'}`} />{names[booking.meeting_status] || booking.meeting_status}</td><td><span className={`dot ${booking.notify_status === 'succeeded' ? 'ok' : booking.notify_status === 'failed' ? 'bad' : 'muted'}`} />{names[booking.notify_status] || booking.notify_status}</td><td><details className="action-record"><summary>查看记录</summary><p>会议：{booking.meeting_id || '尚未创建'}<br />通知：{booking.notify_id || '尚未完成'}</p>{booking.last_error && <p className="error">{booking.last_error}</p>}</details></td></tr>)}</tbody></table></div> : <p className="empty-inline">暂无预约记录，人工批准后即可开始。</p>}</section>
    <div className="scope-note"><Icon name="shield" /><span>这里展示模拟执行结果，不会发送真实邮件、通知或日历邀请。</span></div>
  </div>
}
