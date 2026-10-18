import { useEffect, useMemo, useState } from 'react'
import {
  PolarAngleAxis,
  PolarGrid,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { api } from '../api'
import type { ReviewDetail, ReviewFact } from '../types'

// 维度/事实类型中文名（前端展示层，不暴露后端枚举）
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

// 事实值翻译（档位枚举 → 业务语言）
const VALUE_LABELS: Record<string, string> = {
  one_phase: '负责单个阶段',
  two_phases: '负责两个阶段',
  full_flow: '覆盖至少三个阶段（含交付）',
  participated: '参与相关项目',
  responsible: '承担交付责任',
  led: '主导并有成果',
  collab: '跨国协作',
  delivered: '海外项目交付',
  managed: '管理职责',
  clear_scope: '职责与管理对象明确',
  scope_and_results: '另有范围或结果',
  one_duty: '承担一项职责',
  two_plus: '两项及以上且有举措',
  info: '能力信息',
  usage: '业务使用实例',
  negated: '明确不熟练',
  quantified: '有量化数值',
  location_ok: '接受工作地点',
  travel_ok: '接受出差/驻场',
  true: '是',
  false: '否',
}

const STATUS_LABELS: Record<string, string> = {
  supported: '有据',
  unknown: '待确认',
  contradicted: '矛盾',
  review: '待复核',
}

const HARD_LABELS: Record<string, string> = {
  pass: '通过',
  fail: '未通过',
  unknown: '待确认',
}

const EXTRACTOR_LABELS: Record<string, string> = {
  mock: '规则抽取',
  model: '大模型抽取',
}

function fmtValue(v: unknown): string {
  if (typeof v === 'string') return v
  if (v === null || v === undefined) return ''
  return String(v)
}

function factKey(f: ReviewFact): string {
  return fmtValue(f.value)
}

// 按 kind 分组、同 value 合并页码，去重并转为可读展示
interface FactRow {
  valueLabel: string
  pages: number[]
  quotes: string[]
  status: string // 合并时取更严重者：contradicted > unknown > supported > review
}
interface FactGroup {
  label: string
  rows: FactRow[]
}

const STATUS_RANK: Record<string, number> = {
  contradicted: 3,
  unknown: 2,
  review: 1,
  supported: 0,
}

function groupFacts(facts: ReviewFact[]): FactGroup[] {
  const groups: FactGroup[] = []
  const index = new Map<string, FactGroup>()
  for (const f of facts) {
    const key = f.kind
    let g = index.get(key)
    if (!g) {
      g = { label: KIND_LABELS[key] ?? key, rows: [] }
      index.set(key, g)
      groups.push(g)
    }
    const vk = factKey(f)
    const row = g.rows.find((r) => r.valueLabel === (VALUE_LABELS[vk] ?? vk))
    if (row) {
      if (f.page_number != null && !row.pages.includes(f.page_number)) row.pages.push(f.page_number)
      if (f.quote && !row.quotes.includes(f.quote)) row.quotes.push(f.quote)
      if ((STATUS_RANK[f.status] ?? 0) > (STATUS_RANK[row.status] ?? 0)) row.status = f.status
    } else {
      g.rows.push({
        valueLabel: VALUE_LABELS[vk] ?? vk,
        pages: f.page_number != null ? [f.page_number] : [],
        quotes: f.quote ? [f.quote] : [],
        status: f.status,
      })
    }
  }
  return groups
}

const STATUS_DOT: Record<string, string> = {
  supported: 'ok',
  unknown: 'warn',
  contradicted: 'bad',
  review: 'warn',
}

export default function ReviewPage({ documentId }: { documentId: string }) {
  const [data, setData] = useState<ReviewDetail | null>(null)
  const [error, setError] = useState('')
  const [active, setActive] = useState<{ page: number; quote: string } | null>(null)
  const [view, setView] = useState<'file' | 'text'>('file')
  const [decision, setDecision] = useState('approve')
  const [reason, setReason] = useState('')
  const [approveMsg, setApproveMsg] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    setData(null)
    setError('')
    setApproveMsg('')
    setActive(null)
    setView('file')
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
      setApproveMsg(`${label}已提交：${r['审批状态']}，通知 ${r['通知状态']}`)
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

  const factGroups = useMemo(() => (data ? groupFacts(data.facts) : []), [data])

  // 8 维评分条目（mock 批次下 dimensions 可能为空 dict → 空态占位）
  const dimEntries = useMemo(() => {
    if (!data?.assessment) return []
    return Object.entries(data.assessment.dimensions).map(([key, dim]) => ({
      key,
      label: KIND_LABELS[key] ?? key,
      dim,
    }))
  }, [data])

  const radarData = useMemo(
    () =>
      dimEntries.map(({ key, label, dim }) => ({
        key,
        label,
        score: dim.score ?? 0,
        pct: dim.max_score > 0 ? Math.round(((dim.score ?? 0) / dim.max_score) * 100) : 0,
      })),
    [dimEntries]
  )

  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="hint">加载复核详情…</p>

  // 点击维度：在 matched_facts 中找到第一个能命中 facts 的事实，联动高亮左栏原文
  const linkDim = (key: string) => {
    const dim = data.assessment?.dimensions[key]
    if (!dim) return
    for (const mf of dim.matched_facts ?? []) {
      const hit = data.facts.find((f: ReviewFact) => f.kind === mf || fmtValue(f.value) === mf)
      if (hit && hit.page_number != null && hit.quote) {
        setView('text')
        setActive({ page: hit.page_number, quote: hit.quote })
        return
      }
    }
  }

  const isPdf = data.ext === 'pdf'
  const fileUrl = api.fileUrl(documentId)

  return (
    <div>
      <h1 className="page-title">复核</h1>
      <p className="page-sub">
        {data.file_name}
        {data.assessment && (
          <span className="hint"> · {EXTRACTOR_LABELS[data.assessment.extractor_version] ?? data.assessment.extractor_version}</span>
        )}
      </p>

      <div className="review-grid">
        <div className="doc-pane">
          <div className="view-toggle">
            <button
              type="button"
              className={`seg ${view === 'file' ? 'active' : ''}`}
              onClick={() => setView('file')}
            >
              原始文件
            </button>
            <button
              type="button"
              className={`seg ${view === 'text' ? 'active' : ''}`}
              onClick={() => setView('text')}
            >
              识别文本
            </button>
          </div>
          {view === 'file' ? (
            <div className="page-block">
              <div className="page-tag">
                {active
                  ? `证据在第 ${active.page} 页——请在上方翻到该页核对原文`
                  : isPdf
                    ? '原始 PDF（浏览器内可直接翻页 / 缩放）'
                    : '原始图片'}
              </div>
              {isPdf ? (
                <iframe
                  src={fileUrl}
                  title="原始简历 PDF"
                  className="pdf-frame"
                />
              ) : (
                <img src={fileUrl} alt="原始简历" className="img-frame" />
              )}
            </div>
          ) : (
            <>
              {data.pages.length === 0 && <div className="page-block hint">原文为空（解析失败）</div>}
              {data.pages.map((p) => (
                <div key={p.page_number} className="page-block">
                  <div className="page-tag">第 {p.page_number} 页</div>
                  <div className="page-text">{highlight(p.page_number, p.text)}</div>
                </div>
              ))}
            </>
          )}
        </div>

        <div>
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="card-title">事实与证据</div>
            {factGroups.length === 0 && <div className="hint">未抽取到事实。</div>}
            {factGroups.map((g, gi) => (
              <div key={gi} className="fact-group">
                <div className="fact-group-title">{g.label}</div>
                {g.rows.map((row, ri) => (
                  <div
                    key={ri}
                    className="fact-row"
                    onClick={() =>
                      row.pages.length > 0 &&
                      row.quotes[0] &&
                      setActive({ page: row.pages[0], quote: row.quotes[0] })
                    }
                    title="点击定位证据"
                  >
                    <div className="fact-head">
                      <span className={`dot ${STATUS_DOT[row.status] ?? 'muted'}`} />
                      <span className="fact-value">{row.valueLabel}</span>
                      <span className="fact-status">{STATUS_LABELS[row.status] ?? row.status}</span>
                      {row.pages.length > 0 && (
                        <span className="fact-pages">
                          {row.pages.map((p) => `第 ${p} 页`).join('、')}
                        </span>
                      )}
                    </div>
                    {row.quotes[0] && <div className="fact-quote">“{row.quotes[0]}”</div>}
                  </div>
                ))}
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
              {dimEntries.length > 0 ? (
                <div className="radar-wrap">
                  <ResponsiveContainer width="100%" height={230}>
                    <RadarChart
                      data={radarData}
                      outerRadius="70%"
                      onClick={(state: unknown) => {
                        const s = state as {
                          activePayload?: { payload?: { key?: string } }[]
                        }
                        const key = s?.activePayload?.[0]?.payload?.key
                        if (key) linkDim(key)
                      }}
                    >
                      <PolarGrid stroke="#e3e3de" />
                      <PolarAngleAxis dataKey="label" tick={{ fontSize: 11, fill: '#6d6d66' }} />
                      <Radar
                        dataKey="pct"
                        stroke="#1f5f54"
                        fill="#1f5f54"
                        fillOpacity={0.25}
                        isAnimationActive={false}
                      />
                      <Tooltip formatter={(v: unknown) => [`${v}%`, '得分率']} />
                    </RadarChart>
                  </ResponsiveContainer>
                  <div className="hint" style={{ textAlign: 'center', marginTop: -6 }}>
                    八维得分率（%），点击维度定位原文证据
                  </div>
                </div>
              ) : (
                <div className="hint" style={{ marginBottom: 10 }}>
                  暂无维度评分
                </div>
              )}
              {dimEntries.length > 0 && (
                <div className="dim-list">
                  {dimEntries.map(({ key, label, dim }) => (
                    <div
                      key={key}
                      className="dim-row"
                      onClick={() => linkDim(key)}
                      title="点击定位证据"
                    >
                      <span className={`dot ${STATUS_DOT[dim.status] ?? 'muted'}`} />
                      <span className="dim-name">{label}</span>
                      <span className="dim-score num">
                        {dim.score == null ? '—' : dim.score}/{dim.max_score}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {hardEntries.map(([k, v]) => (
                <div key={k} className="kv">
                  <span className="k">{KIND_LABELS[k] ?? k}</span>
                  <span className="v">{HARD_LABELS[fmtValue(v)] ?? fmtValue(v)}</span>
                </div>
              ))}
              {data.assessment.missing_items.length > 0 && (
                <div className="hint" style={{ marginTop: 8 }}>
                  缺失信息：{data.assessment.missing_items.map((m) => KIND_LABELS[m] ?? m).join('、')}
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
