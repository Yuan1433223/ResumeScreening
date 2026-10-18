import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { TraceList } from '../types'

const RECO: Record<string, string> = {
  recommend: '推荐',
  needs_review: '待复核',
  hold: '暂缓',
}

const DEC: Record<string, { t: string; c: string }> = {
  pending: { t: '待审批', c: 'muted' },
  approve: { t: '已通过', c: 'ok' },
  hold: { t: '已暂缓', c: 'bad' },
  needs_review: { t: '需补充', c: 'warn' },
}

function fmtTs(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

export default function TracePage() {
  const [list, setList] = useState<TraceList | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [batchId, setBatchId] = useState('')
  const [docId, setDocId] = useState('')
  const [docInput, setDocInput] = useState('')
  const [open, setOpen] = useState<Set<number>>(new Set())

  const load = (bid: string, did: string) => {
    setLoading(true)
    setError('')
    api
      .listTraces({ limit: 50, batch_id: bid || undefined, document_id: did || undefined })
      .then(setList)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load('', '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const batches = useMemo(() => {
    const s = new Set<string>()
    ;(list?.rows ?? []).forEach((r) => s.add(r.batch_id))
    return Array.from(s).sort()
  }, [list])

  const toggle = (seq: number) => {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(seq)) next.delete(seq)
      else next.add(seq)
      return next
    })
  }

  const rows = list?.rows ?? []

  return (
    <div>
      <h1 className="page-title">追踪</h1>
      <p className="page-sub">最近 50 条解析/抽取流水，含警告与报错；展开可看原始 JSON。</p>

      {error && <p className="error">{error}</p>}

      <div className="card">
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <label className="inline">
            批次
            <select
              value={batchId}
              onChange={(e) => {
                setBatchId(e.target.value)
                load(e.target.value, docId)
              }}
            >
              <option value="">全部批次</option>
              {batches.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <input
            type="text"
            placeholder="document_id 精确过滤（可选）"
            value={docInput}
            onChange={(e) => setDocInput(e.target.value)}
            style={{ width: 240 }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setDocId(docInput.trim())
                load(batchId, docInput.trim())
              }
            }}
          />
          <button
            className="btn"
            onClick={() => {
              setDocId(docInput.trim())
              load(batchId, docInput.trim())
            }}
          >
            查询
          </button>
          <span className="hint">共 {list?.total ?? 0} 条，倒序排列。</span>
        </div>
      </div>

      {loading && <p className="hint">加载追踪流水…</p>}
      {!loading && rows.length === 0 && !error && <p className="hint">暂无追踪记录。</p>}

      <div className="trace-list">
        {rows.map((r) => {
          const dec = DEC[r.review_decision] ?? { t: r.review_decision || '—', c: 'muted' }
          const isOpen = open.has(r.seq)
          return (
            <div key={r.seq} className="trace-card">
              <div className="trace-head" onClick={() => toggle(r.seq)}>
                <div className="trace-main">
                  <div className="trace-file">
                    {r.file_name || r.document_id}
                    {r.error && <span className="dot bad" style={{ marginLeft: 8 }} />}
                    {r.warnings.length > 0 && !r.error && (
                      <span className="dot warn" style={{ marginLeft: 8 }} />
                    )}
                  </div>
                  <div className="hint trace-sub">
                    {fmtTs(r.ts)} · {r.extractor === 'model' ? '大模型' : '规则'} · 解析{' '}
                    {r.parse_status} · 事实 {r.facts} 条 · 经历 {r.employments} 段
                  </div>
                </div>
                <div className="trace-right">
                  <span className="status">
                    <span className={`dot ${dec.c}`} />
                    {dec.t}
                  </span>
                  <span className="num trace-score">
                    {r.known_score == null ? '—' : r.known_score} 分
                  </span>
                  <span className="tag">{RECO[r.recommendation] ?? r.recommendation ?? '—'}</span>
                </div>
              </div>
              {isOpen && (
                <div className="trace-detail">
                  {Object.keys(r.stats).length > 0 && (
                    <>
                      <div className="fact-group-title">统计 stats</div>
                      {Object.entries(r.stats).map(([k, v]) => (
                        <div key={k} className="kv">
                          <span className="k">{k}</span>
                          <span className="v">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                        </div>
                      ))}
                    </>
                  )}
                  {r.warnings.length > 0 && (
                    <>
                      <div className="fact-group-title">警告 warnings</div>
                      <ul className="trace-warn">
                        {r.warnings.map((w, i) => (
                          <li key={i}>{w}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  {r.error && (
                    <>
                      <div className="fact-group-title" style={{ color: 'var(--bad)' }}>
                        错误 error
                      </div>
                      <pre className="trace-pre">{r.error}</pre>
                    </>
                  )}
                  <div className="fact-group-title">原始记录 JSON</div>
                  <pre className="trace-pre">{JSON.stringify(r, null, 2)}</pre>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
