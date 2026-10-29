import { useEffect, useState } from 'react'
import { api } from '../api'
import Icon from '../components/Icon'
import type { BatchRow, ReviewDetail, ReviewFact } from '../types'

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
  experience: '工作年限',
  summary: '硬条件综合',
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

const DECISIONS: Record<string, string> = { approve: '批准进入排期', hold: '暂缓处理', needs_review: '需要补充信息' }
const fmt = (v: unknown) => v == null ? '' : String(v)
const extractorName = (v: string) => v.startsWith('model') ? '模型抽取' : '规则抽取'
export default function ReviewPage({ documentId, assessmentId = '' }: { documentId: string; assessmentId?: string }) {
  const [data, setData] = useState<ReviewDetail | null>(null)
  const [queue, setQueue] = useState<BatchRow[]>([])
  const [pendingOnly, setPendingOnly] = useState(false)
  const [error, setError] = useState('')
  const [active, setActive] = useState<ReviewFact | null>(null)
  const [view, setView] = useState<'file' | 'text'>('text')
  const [decision, setDecision] = useState('needs_review')
  const [reason, setReason] = useState('')
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const batchId = window.localStorage.getItem('batch_id') || ''
  const loadQueue = () => { if (batchId) api.getBatch(batchId).then(r => setQueue(r.rows)).catch(() => {}) }
  useEffect(() => {
    let cancelled = false
    api.review(documentId, assessmentId).then(r => { if (!cancelled) setData(r) }).catch((e: Error) => { if (!cancelled) setError(e.message) })
    loadQueue()
    return () => { cancelled = true }
    // Only the selected snapshot controls this request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, assessmentId])
  useEffect(() => { if (active && view === 'text') document.getElementById(`evidence-page-${active.page_number}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [active, view])
  const selectFact = (fact: ReviewFact) => { if (fact.quote && fact.page_number != null) { setActive(fact); setView('text') } }
  const highlight = (page: number, text: string) => {
    if (!active || active.page_number !== page || !active.quote) return text
    let start = active.start ?? text.indexOf(active.quote)
    let end = active.end ?? start + active.quote.length
    if (start < 0 || text.slice(start, end) !== active.quote) { start = text.indexOf(active.quote); end = start + active.quote.length }
    if (start < 0) return text
    return <>{text.slice(0, start)}<mark className="hit">{text.slice(start, end)}</mark>{text.slice(end)}</>
  }
  const submit = async () => {
    if (!data?.assessment || submitting) return
    setSubmitting(true); setError(''); setMessage('')
    const saved = data.review
    const storageKey = `review-request:${data.assessment.id}`
    const requestId = saved?.request_id || window.localStorage.getItem(storageKey) || window.crypto.randomUUID()
    window.localStorage.setItem(storageKey, requestId)
    try {
      const result = await api.approve(documentId, saved?.decision || decision, saved?.reason || reason, data.assessment.id, data.assessment.job_version, requestId)
      setMessage(result.recovery_pending ? result.message : '审批已完成，决定与当前证据已保存。')
      setData(await api.review(documentId, data.assessment.id)); loadQueue()
    } catch (e) { setError((e as Error).message) } finally { setSubmitting(false) }
  }
  if (!data) return <div className="loading-state">{error || '正在读取已保存的评估与证据…'}</div>
  const assessment = data.assessment
  const saved = data.review
  const groups = [...new Set(data.facts.map(f => f.kind))]
  const visibleQueue = pendingOnly ? queue.filter(r => r.review_decision === 'pending') : queue
  const currentStatus = saved ? saved.consumed ? DECISIONS[saved.decision] : '审批已保存 · 等待恢复' : '等待人工决定'
  return <div>
    <div className="review-heading"><div><a className="back-link" href="#/batches">← 返回批次</a><h1 className="page-title">证据复核</h1><p className="page-sub">核对岗位证据，再作出人工决定。</p></div><span className={`badge ${saved?.consumed ? 'positive' : 'warning'}`}>{currentStatus}</span></div>
    {error && <div className="notice danger" role="alert">{error}</div>}
    {!data.is_current && <div className="notice warning">正在查看历史评估。已有更新结果时，请从批次列表打开最新评估后审批。</div>}
    {!data.snapshot_available && <div className="notice warning">这份历史评估没有完整证据快照。请重新创建分析批次，查看页面不会调用模型。</div>}
    <div className="review-workspace">
      <aside className="review-queue"><div className="queue-heading"><strong>审核队列</strong><span className="count">{queue.length}</span></div><label className="queue-filter"><input type="checkbox" checked={pendingOnly} onChange={e => setPendingOnly(e.target.checked)} />只看待审批</label><div className="queue-items">{visibleQueue.map(row => <a key={row.run_id} href={`#/review/${encodeURIComponent(row.document_id)}?assessment_id=${encodeURIComponent(row.assessment_id)}`} className={`queue-item ${row.assessment_id === assessment?.id ? 'selected' : ''}`} aria-current={row.assessment_id === assessment?.id ? 'page' : undefined}><span className="queue-file"><Icon name="files" size={16} /><strong>{row.candidate || row.file_name}</strong></span><small>{row.file_name}</small><span><span className={`dot ${row.review_decision === 'pending' ? 'warn' : 'ok'}`} />{row.review_decision === 'pending' ? '待审批' : DECISIONS[row.review_decision] || row.review_decision}</span></a>)}{!visibleQueue.length && <p className="hint queue-empty">{queue.length ? '没有待审批项。' : '从批次列表进入，可在这里切换候选人。'}</p>}</div><a className="queue-back" href="#/batches">查看批次列表 <Icon name="arrow" size={15} /></a></aside>
      <section className="doc-pane"><div className="document-header"><div className="document-file"><Icon name="files" /><div><strong>{data.file_name}</strong><small>{assessment ? extractorName(assessment.extractor_version) : '等待分析'} · 已保存的原文</small></div></div><div className="view-toggle"><button className={`seg ${view === 'text' ? 'active' : ''}`} onClick={() => setView('text')}>识别文本</button><button className={`seg ${view === 'file' ? 'active' : ''}`} onClick={() => setView('file')}>原始文件</button></div></div>
        <div className="evidence-location" role="status">{active ? <><Icon name="check" size={15} />已定位第 {active.page_number} 页 · {KIND_LABELS[active.kind] || active.kind}</> : <><Icon name="search" size={15} />选择右侧证据，定位对应原文</>}</div>
        <div className="document-body">{view === 'text' ? <>{!data.pages.length && <div className="empty-state"><Icon name="files" size={30} /><h3>暂时无法读取正文</h3><p>保留人工处理路径，请查看原文件并补充信息。</p></div>}{data.pages.map(page => <div className="page-block" id={`evidence-page-${page.page_number}`} key={page.page_number}><div className="page-tag">简历原文 <span>第 {page.page_number} 页</span></div><div className="page-text">{highlight(page.page_number, page.text)}</div></div>)}</> : data.ext === 'pdf' ? <iframe className="pdf-frame" title="原始简历 PDF" src={`${api.fileUrl(documentId)}#page=${active?.page_number || 1}`} /> : data.ext === 'docx' ? <div className="empty-state"><h3>Word 原文件</h3><p>请使用识别文本核对证据，或下载原文件。</p><a className="btn" href={api.fileUrl(documentId)}>下载原文件</a></div> : <img className="img-frame" src={api.fileUrl(documentId)} alt="原始简历" />}</div>
      </section>
      <aside className="review-detail">
        <section className="card assessment-summary"><div className="section-heading"><h2 className="card-title">机器建议</h2><span className={`badge ${assessment?.recommendation === 'recommend' ? 'positive' : 'warning'}`}>{assessment?.recommendation === 'recommend' ? '建议推荐' : assessment?.recommendation === 'hold' ? '建议暂缓' : '需要人工核对'}</span></div><div className="score-summary"><strong>{assessment?.known_score ?? '—'}<small> / 100</small></strong><span>{assessment?.score_complete ? '评分信息完整' : '已确认分数 · 存在缺失项'}</span></div><p className="hint">机器建议用于辅助核对，批准由人工决定。</p></section>
        <section className="card"><div className="section-heading"><h2 className="card-title">事实与原文证据</h2><span className="count">{data.facts.length}</span></div><p className="hint">选择引文，左侧定位到同一评估保存的原文。</p>{!groups.length && <p className="empty-inline">未提取到可确认事实，请人工核对。</p>}{groups.map(kind => <div className="fact-group" key={kind}><h3 className="fact-group-title">{KIND_LABELS[kind] || kind}</h3>{data.facts.filter(f => f.kind === kind).map(fact => <button key={fact.id} className={`evidence-card ${active?.id === fact.id ? 'selected' : ''}`} onClick={() => selectFact(fact)} disabled={!fact.quote} aria-label={`${KIND_LABELS[kind] || kind}：${VALUE_LABELS[fmt(fact.value)] || fmt(fact.value)}，定位证据`}><span className="fact-head"><strong>{VALUE_LABELS[fmt(fact.value)] || fmt(fact.value)}</strong><span className={`badge ${fact.status === 'supported' ? 'positive' : 'warning'}`}>{STATUS_LABELS[fact.status] || fact.status}</span></span><q>{fact.quote || '没有可定位引用'}</q><span className="evidence-page">第 {fact.page_number ?? '—'} 页 <Icon name="arrow" size={13} /></span></button>)}</div>)}{data.evidence_valid && <p className="hint evidence-check"><Icon name="check" size={14} />引用位置校验 {data.evidence_valid.valid_count} / {data.evidence_valid.total}；语义仍需人工核对。</p>}</section>
        {assessment && <details className="card dimensions-card" open><summary>八维评分与缺失信息</summary><div className="dim-list">{Object.entries(assessment.dimensions).map(([kind, dim]) => { const fact = data.facts.find(f => dim.matched_facts.includes(f.id)); return <button className="dimension-row" key={kind} onClick={() => fact && selectFact(fact)} disabled={!fact?.quote}><span>{KIND_LABELS[kind] || kind}</span><strong>{dim.score == null ? '待确认' : `${dim.score} / ${dim.max_score}`}</strong><span className={`dimension-track ${dim.score == null ? 'unknown' : ''}`}>{dim.score != null && <span style={{ width: `${dim.score / dim.max_score * 100}%` }} />}</span></button> })}</div><div className="hard-rules">{Object.entries(assessment.hard_results).map(([kind, value]) => <div className="kv" key={kind}><span>{KIND_LABELS[kind] || kind}</span><span>{HARD_LABELS[fmt(value)] || fmt(value)}</span></div>)}</div>{assessment.missing_items.length > 0 && <p className="missing-note">待补充：{assessment.missing_items.map(k => KIND_LABELS[k] || k).join('、')}</p>}</details>}
        {data.warnings.length > 0 && <div className="notice warning"><strong>待核对信息</strong><ul>{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></div>}
        <section className="card approval-card"><span className="eyebrow">人工决定</span><h2>确认下一步处理</h2>{saved ? <><div className="notice positive">{DECISIONS[saved.decision]}{saved.consumed ? ' · 已完成' : ' · 决定已保存'}</div>{saved.reason && <p className="saved-reason">{saved.reason}</p>}{!saved.consumed && <><p className="hint">决定已保存，继续恢复不会重复创建审批。</p><button className="btn primary full-width" onClick={submit} disabled={submitting}>{submitting ? '恢复中…' : '继续恢复流程'}</button></>}</> : <><fieldset disabled={!data.is_current || !data.snapshot_available || submitting}><legend className="sr-only">审批决定</legend>{Object.entries(DECISIONS).map(([key, label]) => <label className={`decision-option ${decision === key ? 'selected' : ''}`} key={key}><input type="radio" name="decision" value={key} checked={decision === key} onChange={() => setDecision(key)} />{label}</label>)}<label className="reason-label" htmlFor="review-reason">审批理由 <span>可选</span></label><textarea id="review-reason" rows={3} value={reason} onChange={e => setReason(e.target.value)} placeholder="记录需要补充的信息或作出决定的依据" /><button className="btn primary full-width" onClick={submit} disabled={!assessment || submitting}>{submitting ? '保存并恢复中…' : '提交人工决定'}<Icon name="check" size={16} /></button></fieldset><p className="hint">决定先保存，再继续流程。批准后需另行发起排期。</p></>}{message && <p className="status-message" role="status">{message}</p>}</section>
        {assessment && <div className="snapshot-note">评估保存于 {new Date(assessment.created_at).toLocaleString('zh-CN')}<br />版本 {assessment.job_version} · 事实与评分使用同一份快照</div>}
      </aside>
    </div>
  </div>
}
