import type {
  ApproveResult,
  BatchTask,
  Dashboard,
  LoginResponse,
  MeResponse,
  ResumeList,
  ReviewDetail,
  Scheduling,
  TraceList,
} from './types'

// 这些端点的 401 由页面自己处理，不触发全局跳转：
// - /api/auth/login：登录失败 401 是正常业务分支，需在登录页显示 detail
// - /api/auth/me：契约规定未认证也返回 200，不会是 401，这里双保险
const NO_REDIRECT_401 = new Set(['/api/auth/login', '/api/auth/me'])

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    // 受保护业务路由未认证 → 跳登录页（登录/me 自身除外，避免死循环）
    if (res.status === 401 && !NO_REDIRECT_401.has(path)) {
      const h = window.location.hash
      if (!h.startsWith('#/login')) window.location.hash = '#/login'
    }
    let detail = `请求失败（HTTP ${res.status}）`
    try {
      const body = await res.json()
      if (body && body.detail) detail = String(body.detail)
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  createBatch: (limit: number, extractor: 'mock' | 'model') =>
    request<BatchTask>('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit, extractor }),
    }),
  getBatchTask: (taskId: string) => request<BatchTask>(`/api/batches/tasks/${taskId}`),
  listResumes: () => request<ResumeList>('/api/resumes'),
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ file_name: string; saved: string }>('/api/upload', { method: 'POST', body: fd })
  },
  review: (documentId: string) => request<ReviewDetail>(`/api/documents/${documentId}/review`),
  /** 原始简历文件地址（PDF/图片直出，复核页左侧呈现） */
  fileUrl: (documentId: string) => `/api/documents/${documentId}/file`,
  approve: (documentId: string, decision: string, reason: string) =>
    request<ApproveResult>(`/api/documents/${documentId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason }),
    }),
  scheduling: () => request<Scheduling>('/api/scheduling'),
  runScheduling: () => request<Scheduling>('/api/scheduling/run', { method: 'POST' }),
  getDashboard: () => request<Dashboard>('/api/dashboard'),
  listTraces: (params: { limit?: number; batch_id?: string; document_id?: string }) => {
    const qs = new URLSearchParams()
    if (params.limit != null) qs.set('limit', String(params.limit))
    if (params.batch_id) qs.set('batch_id', params.batch_id)
    if (params.document_id) qs.set('document_id', params.document_id)
    const s = qs.toString()
    return request<TraceList>(`/api/traces${s ? `?${s}` : ''}`)
  },
  /** SSE 任务进度流地址（供 EventSource 直接使用） */
  taskEventsUrl: (taskId: string) => `/api/batches/tasks/${taskId}/events`,

  // ===== 认证 =====
  login: (username: string, password: string) =>
    request<LoginResponse>('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  getMe: () => request<MeResponse>('/api/auth/me'),
}
