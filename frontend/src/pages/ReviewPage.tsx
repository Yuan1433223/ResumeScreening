import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ReviewDetail } from '../types'

const KIND_LABELS: Record<string, string> = {
  total_years: '总年限',
  management: '管理经验',
  project_scale: '项目规模',
  quality_safety: '质量安全',
  english: '英语能力',
  overseas: '海外经验',
  mobility: '可出差',
  steel_full_flow: '钢结构全流程',
  steel_mic_experience: '钢结构经验',
  relevant_domain: '相关领域',
  relevant_years: '相关年限',
}

const STATUS_DOT: Record<string, string> = {
  supported: 'ok',
  unknown: 'warn',
  contradicted: 'bad',
  review: 'warn',
}

function fmtValue(v: unknown): string {
  if (typeof v === 'string') return v
  return JSON.stringify(v)
}

export default function ReviewPage({ documentId }: { documentId: string }) {
  const [data, setData] = useState<ReviewDetail | null>(null)
  const [error, setError] = useState('')
  const [active, setActive] = useState<{ page: number; quote: string } | null>(null)
  const [decision, setDecision] = useState('approve')
  const [reason, setReason] = useState('')
  const [approveMsg, setApproveMsg] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    setData(null)
    setError('')
    setApproveMsg('')
    setActive(null)
    api
      .review(documentId)
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [documentId])

  const highlight = (pageNumber: number, text: string) => {
    if (!active || active.page !== pageNumber || !active.quote) return text
    const idx = text.indexOf(active.quote)
    if (idx < 0) return text
    return (
      <>
        {text.slice(0, idx)}
        <mark className="hit">{text.slice(idx, idx + active.quote.length)}</mark>
        {text.slice(idx + active.quote.length)}
      </>
    )
  }

  const submit = async () => {
    setSubmitting(true)
    setApproveMsg('')
    try {
      const r = await api.approve(documentId, decision, reason)
      const label = decision === 'approve' ? '批准' : decision === 'hold' ? '暂缓' : '需补充'
      setApproveMsg(`${label}已提交：审批状态 ${r['审批状态']}，通知 ${r['通知状态']}`)
      const fresh = await api.review(documentId)
      setData(fresh)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const hardEntries = useMemo(() => {
    if (!data?.assessment) return []
    return Object.entries(data.assessment.hard_results)
  }, [data])

  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="hint">加载复核详情…</p>

  return (
    <div>
      <h1 className="page-title">复核</h1>
      <p className="page-sub">
        {data.file_name}
        {data.assessment && (
          <span className="hint">
            {' '}
            · 评估版本 {data.assessment.job_version} · 抽取器 {data.assessment.extractor_version}
          </span>
        )}
      </p>

      <div className="review-grid">
        <div className="doc-pane">
          {data.pages.length === 0 && <div className="page-block hint">原文为空（解析失败）</div>}
          {data.pages.map((p) => (
            <div key={p.page_number} className="page-block">
              <div className="page-tag">第 {p.page_number} 页</div>
              <div className="page-text">{highlight(p.page_number, p.text)}</div>
            </div>
          ))}
        </div>

        <div>
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="card-title">事实与证据</div>
            {data.facts.length === 0 && <div className="hint">未抽取到事实。</div>}
            {data.facts.map((f, i) => (
              <div
                key={i}
                className="fact-row"
                onClick={() =>
                  f.page_number != null &&
                  f.quote &&
                  setActive({ page: f.page_number, quote: f.quote })
                }
                title="点击在原文中定位"
              >
                <div className="fact-head">
                  <span className={`dot ${STATUS_DOT[f.status] ?? 'muted'}`} />
                  <span className="fact-kind">{KIND_LABELS[f.kind] ?? f.kind}</span>
                  <span className="fact-value">{fmtValue(f.value)}</span>
                </div>
                {f.quote && (
                  <div className="fact-quote">
                    “{f.quote}”{f.page_number != null && `（第 ${f.page_number} 页）`}
                  </div>
                )}
              </div>
            ))}
            {data.evidence_valid && (
              <div className="hint" style={{ marginTop: 10 }}>
                证据校验 {data.evidence_valid.valid_count}/{data.evidence_valid.total} 通过
              </div>
            )}
            {data.warnings.length > 0 && (
              <div className="hint" style={{ marginTop: 6 }}>
                警告：{data.warnings[0]}
              </div>
            )}
          </div>

          {data.assessment && (
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-title">
                评估明细
                <span className="num" style={{ float: 'right', color: 'var(--accent)' }}>
                  {data.assessment.known_score == null ? '—' : data.assessment.known_score} 分
                </span>
              </div>
              {hardEntries.map(([k, v]) => (
                <div key={k} className="kv">
                  <span className="k">{KIND_LABELS[k] ?? k}</span>
                  <span className="v">{fmtValue(v)}</span>
                </div>
              ))}
              {data.assessment.missing_items.length > 0 && (
                <div className="hint" style={{ marginTop: 8 }}>
                  缺失：{data.assessment.missing_items.join('、')}
                </div>
              )}
            </div>
          )}

          <div className="card">
            <div className="card-title">审批</div>
            <div className="toolbar">
              <select value={decision} onChange={(e) => setDecision(e.target.value)}>
                <option value="approve">批准</option>
                <option value="hold">暂缓</option>
                <option value="needs_review">需补充</option>
              </select>
              <input
                type="text"
                placeholder="理由（可选）"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={{ flex: 1, minWidth: 160 }}
              />
            </div>
            <div className="approve-bar">
              <button className="btn primary" onClick={submit} disabled={submitting}>
                {submitting ? '提交中…' : '提交审批'}
              </button>
              {approveMsg && <span className="hint">{approveMsg}</span>}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
