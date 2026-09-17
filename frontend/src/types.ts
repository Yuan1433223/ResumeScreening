// API 返回类型（与 app/api.py 对齐）

export interface BatchRow {
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
  status: 'running' | 'done'
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

export interface ReviewDetail {
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
    job_version: string
    hard_results: Record<string, unknown>
    dimensions: Record<string, unknown>
    known_score: number | null
    score_complete: boolean
    missing_items: string[]
    recommendation: string
    extractor_version: string
  } | null
}

export interface ApproveResult {
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
  booking_id: string
  candidate: string
  document_id: string
  slot_id: string | null
  status: string
  notify_status: string
}

export interface Scheduling {
  batch_id: string | null
  round: { id: string; round_no: number; status: string } | null
  slots: Slot[]
  bookings: Booking[]
}
