import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
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
  const [resumes, setResumes] = useState<ResumeList | null>(null)
  const [task, setTask] = useState<BatchTask | null>(null)
  const [limit, setLimit] = useState(10)
  const [extractor, setExtractor] = useState<'mock' | 'model'>('mock')
  const [running, setRunning] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [uploadMsg, setUploadMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [doneMsg, setDoneMsg] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const pollRef = useRef<number | null>(null)

  const loadResumes = () => {
    api.listResumes().then(setResumes).catch((e: Error) => setError(e.message))
  }

  // 刷新页面后恢复上次批次任务的轮询（任务存于后端内存表，服务重启则失效）
  useEffect(() => {
    loadResumes()
    const saved = window.localStorage.getItem('batch_task_id')
    if (saved) {
      api
        .getBatchTask(saved)
        .then((t) => {
          setTask(t)
          if (t.status === 'running') startPoll(t.task_id)
        })
        .catch(() => window.localStorage.removeItem('batch_task_id'))
    }
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current)
    }
  }, [])

  const stopPoll = () => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  const startPoll = (taskId: string) => {
    stopPoll()
    pollRef.current = window.setInterval(async () => {
      try {
        const cur = await api.getBatchTask(taskId)
        setTask(cur)
        if (cur.status !== 'running') {
          stopPoll()
          window.localStorage.removeItem('batch_task_id')
          setDoneMsg(`批次完成：${cur.done}/${cur.total} 份已处理。`)
        }
      } catch (e) {
        setError((e as Error).message)
        stopPoll()
        window.localStorage.removeItem('batch_task_id')
      }
    }, 2000)
  }

  const runBatch = async () => {
    setRunning(true)
    setError('')
    setDoneMsg('')
    try {
      const t = await api.createBatch(limit, extractor)
      setTask(t)
      window.localStorage.setItem('batch_task_id', t.task_id)
      startPoll(t.task_id)
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
    for (const f of Array.from(files)) {
      try {
        const r = await api.upload(f)
        ok.push(r.file_name)
      } catch (e) {
        bad.push(`${f.name}：${(e as Error).message}`)
      }
    }
    if (ok.length > 0) {
      loadResumes()
      setUploadMsg({
        ok: bad.length === 0,
        text: bad.length === 0 ? `已上传 ${ok.length} 份` : `已上传 ${ok.length} 份，${bad.length} 份失败`,
      })
    } else {
      setUploadMsg({ ok: false, text: `上传失败：${bad.join('；')}` })
    }
    setUploading(false)
    if (fileRef.current) fileRef.current.value = ''
  }

  const rows = task && task.status === 'done' ? task.rows : []

  return (
    <div>
      <h1 className="page-title">批次</h1>
      <p className="page-sub">上传简历，运行筛选批次；审批状态与评分结果见下表。</p>

      {error && <p className="error">{error}</p>}
      {uploadMsg && (
        <p className={uploadMsg.ok ? 'ok' : 'error'}>{uploadMsg.text}</p>
      )}

      <div className="card">
        <div className="card-title">简历库（{resumes ? resumes.total : '…'} 份）</div>
        <div className="toolbar">
          <label className="inline">
            文件数
            <input
              type="number"
              min={1}
              max={100}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 10)}
              style={{ width: 84 }}
              aria-label="批次文件数上限"
            />
          </label>
          <label className="inline">
            抽取器
            <select value={extractor} onChange={(e) => setExtractor(e.target.value as 'mock' | 'model')}>
              <option value="mock">规则</option>
              <option value="model">大模型</option>
            </select>
          </label>
          <button className="btn primary" onClick={runBatch} disabled={running || (task?.status === 'running')}>
            {task?.status === 'running' ? '运行中…' : '创建批次'}
          </button>
          <button className="btn" onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? '上传中…' : '上传简历'}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.jpg,.jpeg,.png,.bmp"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => onUpload(e.target.files)}
          />
          <span className="hint">批次后台执行，模型抽取较慢；同内容文件自动去重。</span>
        </div>
      </div>

      {task && task.status === 'running' && (
        <div className="card">
          <div className="card-title">
            批次运行中 · {task.extractor === 'model' ? '大模型' : '规则'}抽取 · {task.done}/{task.total}
          </div>
          <div
            role="progressbar"
            aria-valuenow={task.total ? Math.round((task.done / task.total) * 100) : 0}
            aria-valuemin={0}
            aria-valuemax={100}
            style={{
              height: 4,
              background: 'var(--border)',
              borderRadius: 2,
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${task.total ? Math.round((task.done / task.total) * 100) : 0}%`,
                height: '100%',
                background: 'var(--accent)',
                transition: 'width .4s',
              }}
            />
          </div>
          {task.current_file && (
            <div className="hint" style={{ marginTop: 8 }}>
              正在处理：{task.current_file}
            </div>
          )}
          {task.rows.length > 0 && (
            <div className="hint" style={{ marginTop: 8 }}>
              已处理 {task.rows.length} 份，可先开始复核。
            </div>
          )}
        </div>
      )}
      {doneMsg && <p className="ok">{doneMsg}</p>}

      {rows.length > 0 && (
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>候选人</th>
                <th>评分</th>
                <th>建议</th>
                <th>审批</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const reco = RECO[r.recommendation] ?? { t: r.recommendation, c: 'muted' }
                const dec = DEC[r.review_decision] ?? { t: r.review_decision, c: 'muted' }
                return (
                  <tr key={r.run_id}>
                    <td>
                      <div>{r.candidate || r.file_name}</div>
                      <div className="hint">{r.file_name}</div>
                    </td>
                    <td className="num">
                      {r.known_score == null ? '—' : r.known_score}
                      {!r.score_complete && <span className="hint">（部分）</span>}
                    </td>
                    <td>
                      <span className="status">
                        <span className={`dot ${reco.c}`} />
                        {reco.t}
                      </span>
                    </td>
                    <td>
                      <span className="status">
                        <span className={`dot ${dec.c}`} />
                        {dec.t}
                      </span>
                    </td>
                    <td>
                      <a className="link" href={`#/review/${encodeURIComponent(r.document_id)}`}>
                        复核
                      </a>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
