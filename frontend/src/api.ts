import type { ApproveResult, BatchResponse, ResumeList, ReviewDetail, Scheduling } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
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
  createBatch: (limit: number) =>
    request<BatchResponse>('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit }),
    }),
  listResumes: () => request<ResumeList>('/api/resumes'),
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ file_name: string; saved: string }>('/api/upload', { method: 'POST', body: fd })
  },
  review: (documentId: string) => request<ReviewDetail>(`/api/documents/${documentId}/review`),
  approve: (documentId: string, decision: string, reason: string) =>
    request<ApproveResult>(`/api/documents/${documentId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason }),
    }),
  scheduling: () => request<Scheduling>('/api/scheduling'),
  runScheduling: () => request<Scheduling>('/api/scheduling/run', { method: 'POST' }),
}
