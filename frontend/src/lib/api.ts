import type {
  DashboardSummary,
  DecisionResponse,
  DeliveryReport,
  ExtractedRow,
  InboxPreview,
  ReconciliationDetail,
  ReconciliationRow,
  VendorSendResult,
  VendorTemplate,
} from '@/types'

const BASE = '/api'

/** No auth in this phase; decisions are attributed via this header. */
const OPERATOR = 'ashford@consciouscreations.ai'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Operator': OPERATOR, ...init?.headers },
  })

  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail` — that is what a reviewer needs to
    // see when an approval is refused, so surface it rather than a generic failure.
    let detail = response.statusText
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
      else if (Array.isArray(body?.detail)) detail = body.detail[0]?.msg ?? detail
    } catch {
      /* non-JSON error body; keep statusText */
    }
    throw new ApiError(detail, response.status)
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

export interface ApprovePayload {
  po_line_id?: number | null
  filled_fields?: Record<string, unknown>
  note?: string
}

export const api = {
  summary: () => request<DashboardSummary>('/dashboard/summary'),

  exceptions: () => request<ReconciliationRow[]>('/reconciliation/exceptions'),
  reconciliation: (params: { review_status?: string; confidence?: string; flagged?: boolean } = {}) => {
    const query = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== '') query.set(key, String(value))
    })
    const suffix = query.toString()
    return request<ReconciliationRow[]>(`/reconciliation${suffix ? `?${suffix}` : ''}`)
  },
  detail: (id: number) => request<ReconciliationDetail>(`/reconciliation/${id}`),
  approve: (id: number, payload: ApprovePayload) =>
    post<DecisionResponse>(`/reconciliation/${id}/approve`, payload),
  cancel: (id: number, reason: string) =>
    post<DecisionResponse>(`/reconciliation/${id}/cancel`, { reason }),
  requestInfo: (id: number) => post<VendorSendResult>(`/reconciliation/${id}/request-info`),

  extracted: (params: { status?: string; q?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.status) query.set('status', params.status)
    if (params.q) query.set('q', params.q)
    const suffix = query.toString()
    return request<ExtractedRow[]>(`/extracted-records${suffix ? `?${suffix}` : ''}`)
  },

  deliveryReport: () => request<DeliveryReport>('/delivery-report'),

  inboxPreview: () => request<InboxPreview>('/inbox/preview'),
  organize: (emailIds?: string[]) =>
    post<{ moved: number; remaining: number; by_folder: Record<string, number> }>(
      '/inbox/organize',
      { email_ids: emailIds ?? null },
    ),

  vendorTemplate: () => request<VendorTemplate>('/vendor/template'),
  saveTemplate: (body: Partial<VendorTemplate>) =>
    request<VendorTemplate>('/vendor/template', { method: 'PUT', body: JSON.stringify(body) }),
  setSchedule: (everyNHours: number, enabled: boolean) =>
    post<{ enabled: boolean; every_n_hours: number; next_run_at: string | null }>(
      '/vendor/template/schedule',
      { every_n_hours: everyNHours, enabled },
    ),
  sendTemplate: (extractedRecordId?: number) =>
    post<VendorSendResult>('/vendor/template/send', { extracted_record_id: extractedRecordId ?? null }),

  resetDemo: () => post<{ seeded: boolean; counts: Record<string, number> }>('/admin/reset'),
}

/** Centralised so a decision can invalidate exactly the views it affects. */
export const queryKeys = {
  summary: ['summary'] as const,
  exceptions: ['reconciliation', 'exceptions'] as const,
  reconciliation: ['reconciliation'] as const,
  detail: (id: number) => ['reconciliation', 'detail', id] as const,
  extracted: (params: unknown) => ['extracted', params] as const,
  delivery: ['delivery'] as const,
  inbox: ['inbox'] as const,
  template: ['vendor', 'template'] as const,
}
