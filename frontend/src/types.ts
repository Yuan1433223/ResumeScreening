// API 返回类型（与 app/api.py 对齐）

export interface BatchRow {
  assessment_id: string
  last_error?: string | null
  run_id: string
  document_id: string
  candidate: string
  file_name: string
  parse_status: string
  extractor_version: string
  hard_results: Record<string, unknown>
  known_score: number | null
  score_complete: boolean
  recommendation: string
  missing_items: string[]
  review_decision: string
}

export interface BatchResponse {
  batch_id: string
  total: number
  recommended: number
  review_pending: number
  rows: BatchRow[]
}

export interface BatchTask {
  task_id: string
  batch_id: string
  extractor: 'mock' | 'model'
  status: 'running' | 'done' | 'failed' | 'interrupted'
  total: number
  done: number
  current_file: string
  rows: BatchRow[]
  error: string
}

export interface ResumeList {
  total: number
  files: { file_name: string; size_bytes: number; ext: string }[]
}

export interface ReviewFact {
  id: string
  start: number | null
  end: number | null
  kind: string
  value: unknown
  status: string
  quote: string
  page_number: number | null
}

export interface ReviewPage {
  page_number: number
  text: string
}

// 单个评估维度的得分结构（后端 assessment.dimensions 序列化形状）
export interface DimScore {
  score: number | null
  max_score: number
  status: string
  level: string | null
  matched_facts: string[]
}

export interface ReviewDetail {
  snapshot_available: boolean
  is_current: boolean
  run_id: string | null
  review: { id: string; request_id: string; decision: string; reason: string; consumed: boolean } | null
  document_id: string
  file_name: string
  ext: string
  pages: ReviewPage[]
  facts: ReviewFact[]
  employments: {
    start_month: string | null
    end_month: string | null
    is_current: boolean
    relevant: boolean
  }[]
  evidence_valid: { valid_count: number; total: number } | null
  warnings: string[]
  assessment: {
    analysis_date: string
    id: string
    created_at: string
    job_version: string
    hard_results: Record<string, unknown>
    dimensions: Record<string, DimScore>
    known_score: number | null
    score_complete: boolean
    missing_items: string[]
    recommendation: string
    extractor_version: string
  } | null
}

export interface ApproveResult {
  recovery_pending: boolean
  message: string
  review_id: string
  decision: string
  审批状态: string
  面试安排: boolean
  通知状态: string
  综合评分: number | null
  机器建议: string
}

export interface Slot {
  slot_id: string
  start: string
  end: string
  status: 'open' | 'taken'
  candidate?: string
  document_id?: string
}

export interface Booking {
  meeting_status: string
  meeting_id: string | null
  notify_id: string | null
  last_error: string | null
  booking_id: string
  candidate: string
  document_id: string
  slot_id: string | null
  status: string
  notify_status: string
}

export interface Scheduling {
  approved_count?: number
  pending_count?: number
  batch_id: string | null
  round: { id: string; round_no: number; status: string } | null
  slots: Slot[]
  bookings: Booking[]
}

// ===== 看板 Dashboard =====
export interface KpiSet {
  total_documents: number
  total_batches: number
  recommended: number
  needs_review: number
  hold: number
  approved: number
  avg_score: number | null
  review_rate: number
}

export interface ScoreBucket {
  bucket: string
  count: number
}

export interface LevelBucket {
  level: number
  count: number
}

export interface FunnelStage {
  stage: string
  label: string
  count: number
}

export interface Dashboard {
  kpis: KpiSet
  score_distribution: {
    total_score: ScoreBucket[]
    dimensions: Record<string, LevelBucket[]>
  }
  funnel: FunnelStage[]
}

// ===== Trace 追踪 =====
export interface TraceRow {
  event?: string
  consumed?: boolean
  ts: string
  seq: number
  batch_id: string
  task_id: string
  extractor: 'mock' | 'model'
  document_id: string
  file_name: string
  parse_status: string
  facts: number
  employments: number
  stats: Record<string, unknown>
  warnings: string[]
  review_decision: string
  known_score: number | null
  recommendation: string
  error?: string | null
}

export interface TraceList {
  total: number
  rows: TraceRow[]
}

// ===== 认证 Auth =====
// POST /api/auth/login 成功响应
export interface LoginResponse {
  ok: boolean
  username: string
}

// GET /api/auth/me 响应（未认证也返回 200，authenticated=false）
export interface MeResponse {
  authenticated: boolean
  username: string | null
  auth_enabled: boolean
}
