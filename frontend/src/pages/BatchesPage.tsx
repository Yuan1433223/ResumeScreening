import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'
import type { BatchTask, ResumeList } from '../types'

const RECO: Record<string, { t: string; c: string }> = {
  recommend: { t: '推荐', c: 'ok' },
  needs_review: { t: '待复核', c: 'warn' },
  hold: { t: '暂缓', c: 'bad' },
}

const DEC: Record<string, { t: string; c: string }> = {
  pending: { t: '待审批', c: 'muted' },
  approve: { t: '已通过', c: 'ok' },
  hold: { t: '已暂缓', c: 'bad' },
  needs_review: { t: '需补充', c: 'warn' },
}

export default function BatchesPage() {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [resumes, setResumes] = useState<ResumeList | null>(null)
  const [task, setTask] = useState<BatchTask | null>(null)
  const [limit, setLimit] = useState(10)
  const [extractor, setExtractor] = useState<'mock' | 'model'>('mock')
  const [running, setRunning] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [uploadMsg, setUploadMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [doneMsg, setDoneMsg] = useState('')
  const [sseLive, setSseLive] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const pollRef = useRef<number | null>(null)
  const sseRef = useRef<EventSource | null>(null)

  const loadResumes = () => {
    api.listResumes().then(setResumes).catch((e: Error) => setError(e.message))
  }

  const stopPoll = () => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  const closeSse = () => {
    if (sseRef.current) {
      sseRef.current.onmessage = null
      sseRef.current.onerror = null
      sseRef.current.close()
      sseRef.current = null
    }
    setSseLive(false)
  }

  // 任务收尾：停轮询/SSE、清本地缓存、提示完成
  const finishTask = (t: BatchTask) => {
    stopPoll()
    closeSse()
    setDoneMsg(t.status === 'done' ? `批次完成：${t.done}/${t.total} 份已处理，可开始核对证据。` : '')
    if (t.error) setError(t.error)
  }

  const startPoll = (taskId: string) => {
    closeSse()
    stopPoll()
    pollRef.current = window.setInterval(async () => {
      try {
        const cur = await api.getBatchTask(taskId)
        setTask(cur)
        if (cur.status !== 'running') {
          finishTask(cur)
        }
      } catch (e) {
        setError((e as Error).message)
        stopPoll()
        window.localStorage.removeItem('batch_task_id')
      }
    }, 2000)
  }

  // SSE 实时进度；不支持/连接失败时回退 2s 轮询
  const startSse = (taskId: string) => {
    closeSse()
    stopPoll()
    if (typeof EventSource === 'undefined') {
      startPoll(taskId)
      return
    }
    let es: EventSource
    try {
      es = new EventSource(api.taskEventsUrl(taskId))
    } catch {
      startPoll(taskId)
      return
    }
    sseRef.current = es
    setSseLive(true)

    es.addEventListener('progress', (ev: MessageEvent) => {
      try {
        const t = JSON.parse(String(ev.data)) as BatchTask
        setTask(t)
      } catch {
        /* 忽略损坏帧 */
      }
    })

    es.addEventListener('done', (ev: MessageEvent) => {
      let final: BatchTask | null = null
      try {
        final = JSON.parse(String(ev.data)) as BatchTask
      } catch {
        /* 无 snapshot 时回源拉一次 */
      }
      if (final) {
        setTask(final)
        finishTask(final)
      } else {
        api
          .getBatchTask(taskId)
          .then((cur) => {
            setTask(cur)
            finishTask(cur)
          })
          .catch(() => closeSse())
      }
    })

    es.addEventListener('error', (ev: MessageEvent) => {
      let detail = '任务进度流错误'
      try {
        const body = JSON.parse(String(ev.data)) as { detail?: string }
        if (body.detail) detail = body.detail
      } catch {
        /* 非 JSON */
      }
      setError(detail)
      closeSse()
      startPoll(taskId)
    })

    // 连接级失败（服务不可达/被关闭）→ 回退轮询
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED || es.readyState === EventSource.CONNECTING) {
        closeSse()
        startPoll(taskId)
      }
    }
  }

  // 刷新页面后恢复上次批次任务（SSE 无历史，恢复统一走轮询）
  useEffect(() => {
    loadResumes()
    const saved = window.localStorage.getItem('batch_task_id')
    if (saved) {
      api
        .getBatchTask(saved)
        .then((t) => {
          setTask(t)
          if (t.status === 'running') startPoll(t.task_id)
          else finishTask(t)
        })
        .catch(() => window.localStorage.removeItem('batch_task_id'))
    }
    return () => {
      stopPoll()
      closeSse()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runBatch = async () => {
    setRunning(true)
    setError('')
    setDoneMsg('')
    try {
      const t = await api.createBatch(limit, extractor)
      setTask(t)
      window.localStorage.setItem('batch_task_id', t.task_id)
      window.localStorage.setItem('batch_id', t.batch_id)
      startSse(t.task_id)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  const onUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    setError('')
    setUploadMsg(null)
    const ok: string[] = []
    const bad: string[] = []
    const warnings: string[] = []
    for (const f of Array.from(files)) {
      try {
        const r = await api.upload(f)
        ok.push(r.file_name)
        warnings.push(...r.pii_warnings)
      } catch (e) {
        bad.push(`${f.name}：${(e as Error).message}`)
      }
    }
    if (ok.length > 0) {
      loadResumes()
      setUploadMsg({
        ok: bad.length === 0,
        text: `已上传 ${ok.length} 份${bad.length ? `，失败：${bad.join('；')}` : ''}${warnings.length ? `。${warnings.join('；')}` : ''}`,
      })
    } else {
      setUploadMsg({ ok: false, text: `上传失败：${bad.join('；')}` })
    }
    setUploading(false)
    if (fileRef.current) fileRef.current.value = ''
  }

  const rows = task?.rows || []
  const visibleRows = rows.filter(row => (!search || `${row.candidate} ${row.file_name}`.toLowerCase().includes(search.toLowerCase())) && (filter === 'all' || (filter === 'pending' ? row.review_decision === 'pending' : row.review_decision === 'approve')))
  const pending = rows.filter(r => r.review_decision === 'pending').length
  const approved = rows.filter(r => r.review_decision === 'approve').length
  return <div>
    <div className="page-heading"><div><div className="eyebrow">审核工作空间</div><h1 className="page-title">批次与复核</h1><p className="page-sub">导入简历，核对岗位证据，再提交人工决定。</p></div><button className="btn" onClick={() => fileRef.current?.click()} disabled={uploading}><Icon name="files" />{uploading ? '上传中…' : '上传简历'}</button></div>
    {error && <div className="notice danger" role="alert">{error}</div>}
    {uploadMsg && <div className={`notice ${uploadMsg.ok ? 'positive' : 'warning'}`} role="status">{uploadMsg.text}</div>}
    <input ref={fileRef} type="file" accept=".pdf,.jpg,.jpeg,.png,.bmp,.docx" multiple hidden onChange={e => onUpload(e.target.files)} />
    <section className="card batch-setup"><div><h2 className="card-title">开始一次分析</h2><p className="hint">简历库 <strong>{resumes?.total ?? '—'}</strong> 份 · 支持 PDF、图片及 Word</p></div><div className="toolbar"><label className="inline">文件数上限<input type="number" min={1} max={100} value={limit} onChange={e => setLimit(Math.max(1, Math.min(100, Number(e.target.value) || 1)))} aria-label="批次文件数上限" /></label><label className="inline">事实抽取<select aria-label="事实抽取方式" value={extractor} onChange={e => setExtractor(e.target.value as 'mock' | 'model')}><option value="mock">本地规则</option><option value="model">大模型</option></select></label><button className="btn primary" onClick={runBatch} disabled={running || task?.status === 'running' || !resumes?.total}>{running || task?.status === 'running' ? '分析进行中…' : task?.status === 'interrupted' || task?.status === 'failed' ? '重新分析' : '创建分析批次'}<Icon name="arrow" size={16} /></button></div><p className="setup-note">{extractor === 'model' ? '模型模式会将简历正文发送给配置的模型服务。' : '本地规则模式无需模型密钥。'} 分析只生成建议，所有候选人都保留人工审批。</p></section>
    {task?.status === 'running' && <div className="card task-progress"><div className="section-heading"><strong>正在分析 · {task.done} / {task.total} 份</strong><span className="badge neutral">{sseLive ? '实时更新' : '进度同步中'}</span></div><div className="progress-track" role="progressbar" aria-label="批次分析进度" aria-valuenow={task.total ? Math.round(task.done / task.total * 100) : 0} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${task.total ? task.done / task.total * 100 : 0}%` }} /></div><p className="hint">{task.current_file ? `正在处理：${task.current_file}` : '等待处理…'} · 已完成项可以先开始复核。</p></div>}
    {doneMsg && <div className="notice positive" role="status"><Icon name="check" />{doneMsg}</div>}
    <div className="batch-metrics"><div><span>当前结果</span><strong>{rows.length}<small>份</small></strong></div><div><span>等待人工决定</span><strong>{pending}<small>份</small></strong></div><div><span>人工已批准</span><strong>{approved}<small>份</small></strong></div><p>机器建议与人工决定分别记录，<br />缺少信息的分数不会伪装成完整评分。</p></div>
    <section className="results-panel"><div className="results-toolbar"><div><h2>候选人复核</h2><span className="hint">{task ? '当前分析批次' : '创建批次后，候选人将显示在这里'}</span></div><div className="result-controls"><label className="search-field"><Icon name="search" size={16} /><input type="text" aria-label="搜索候选人" placeholder="搜索姓名或文件名" value={search} onChange={e => setSearch(e.target.value)} /></label><select aria-label="筛选审批状态" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部状态</option><option value="pending">待审批</option><option value="approved">已批准</option></select></div></div>
      {visibleRows.length ? <div className="table-wrap"><table className="grid"><thead><tr><th>候选人 / 简历</th><th>已确认得分</th><th>机器建议</th><th>人工决定</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{visibleRows.map(row => { const reco = RECO[row.recommendation] || { t: row.recommendation, c: 'muted' }; const dec = DEC[row.review_decision] || { t: row.review_decision, c: 'muted' }; return <tr key={row.run_id}><td><div className="candidate-cell"><span className="file-avatar"><Icon name="files" size={18} /></span><span><strong>{row.candidate || row.file_name}</strong><small>{row.file_name}</small>{row.last_error && <small className="error">{row.last_error}</small>}</span></div></td><td><strong className="table-score">{row.known_score ?? '—'}</strong><span className="hint"> / 100</span>{!row.score_complete && <small className="incomplete-label">信息不完整</small>}</td><td><span className={`badge ${reco.c === 'ok' ? 'positive' : reco.c === 'bad' ? 'neutral' : 'warning'}`}>{reco.t}</span></td><td><span className="status"><span className={`dot ${dec.c}`} />{dec.t}</span></td><td>{row.assessment_id ? <a className="btn small" href={`#/review/${encodeURIComponent(row.document_id)}?assessment_id=${encodeURIComponent(row.assessment_id)}`}>核对证据 <Icon name="arrow" size={14} /></a> : <span className="hint">处理失败，请重新分析</span>}</td></tr> })}</tbody></table></div> : <div className="empty-state"><span className="empty-icon"><Icon name="files" size={28} /></span><h3>{rows.length ? '没有符合条件的候选人' : '从一份简历开始核对'}</h3><p>{rows.length ? '调整搜索或筛选条件，查看其他结果。' : '上传简历并创建分析批次，事实、原文和人工审批会在这里汇合。'}</p>{!resumes?.total && <button className="btn" onClick={() => fileRef.current?.click()}>选择简历文件</button>}</div>}
      {rows.length > 0 && <div className="results-footer"><span>共 {rows.length} 份 · {pending} 份等待人工决定</span><a className="link" href="#/scheduling">查看排期与重试 <Icon name="arrow" size={14} /></a></div>}
    </section>
  </div>
}
