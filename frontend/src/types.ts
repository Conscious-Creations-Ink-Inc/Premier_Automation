/**
 * Mirrors the FastAPI schemas. `npm run gen:api` regenerates a full OpenAPI type file from a
 * running server; these hand-written types are the working surface the components use, kept
 * deliberately small and identical in vocabulary to `pipeline/models.py`.
 */

export type Confidence = 'high' | 'medium' | 'low' | 'none'
export type ReviewStatus = 'auto_approved' | 'pending_review' | 'approved' | 'cancelled'

export interface POLine {
  id: number
  po_number: string
  line_number: number
  line_key: string
  spec_code: string
  description: string
  vendor_name: string
  unit_of_measure: string
  qty_ordered: number
  qty_received: number
  qty_outstanding: number
  cost_code: string
  project_code: string
  project_name: string
  line_status: string
  expected_date?: string | null
  ship_to?: string | null
  assigned_agent?: string | null
  pay_terms?: string | null
}

export interface ExtractedRecord {
  source_email_id: string
  po_number?: string | null
  shipment_number?: string | null
  spec_code?: string | null
  parent_spec_code?: string | null
  sub_spec_suffix?: string | null
  item_description?: string | null
  vendor_name?: string | null
  carrier_name?: string | null
  tracking_number?: string | null
  quantity_received?: number | null
  unit_of_measure?: string | null
  pod_stated_date?: string | null
  email_date: string
  delivery_location?: string | null
  comments?: string | null
  extraction_source: string
  extraction_confidence: number
  raw_snippet?: string | null
}

export interface ExtractedRecordRow {
  id: number
  status: string
  created_at: string
  updated_at?: string | null
  record: ExtractedRecord
}

export interface EmailContext {
  email_id: string
  subject: string
  sender_address: string
  received_at: string
  notification_type: string
  triage_category: string
  status_keyword: string
  has_attachment: boolean
}

export interface Match {
  id: number
  extracted_record_id: number
  po_line_id?: number | null
  po_signal: boolean
  spec_signal: boolean
  desc_signal: boolean
  desc_score: number
  signals_matched: number
  confidence: Confidence
  missing_fields: string[]
  flagged: boolean
  flag_reason: string
  route_target: string
  review_status: ReviewStatus
  notes: string
  created_at: string
  updated_at?: string | null
}

export interface Candidate {
  po_line: POLine
  po_signal: boolean
  spec_signal: boolean
  desc_signal: boolean
  desc_score: number
  signals_matched: number
  confidence: Confidence
}

export interface Decision {
  id: number
  decision: string
  decided_by: string
  decided_at: string
  reason: string
  resulting_status: string
  po_line_id?: number | null
  receipt_id?: number | null
}

export interface ReconciliationRow {
  match: Match
  record: ExtractedRecordRow
  po_line?: POLine | null
  email?: EmailContext | null
}

export interface ReconciliationDetail extends ReconciliationRow {
  candidates: Candidate[]
  decisions: Decision[]
  receipt?: Record<string, unknown> | null
  editable_fields: string[]
}

export interface ExtractedRow {
  record: ExtractedRecordRow
  match?: Match | null
  po_line?: POLine | null
  email?: EmailContext | null
}

export interface FunnelStage {
  key: string
  label: string
  count: number
}

export interface DashboardSummary {
  generated_at: string
  totals: Record<string, number>
  funnel: FunnelStage[]
  by_status: Record<string, number>
  by_confidence: Record<string, number>
  by_review_status: Record<string, number>
  by_route_target: Record<string, number>
  exception_queue_size: number
}

export interface DeliveryRow {
  email_id: string
  received_at: string
  subject: string
  sender_address: string
  po_number?: string | null
  notification_type: string
  triage_category: string
  status_keyword: string
  proposed_folder: string
  has_attachment: boolean
}

export interface DeliveryBucket {
  key: string
  label: string
  count: number
  rows: DeliveryRow[]
}

export interface DeliveryReport {
  total: number
  buckets: DeliveryBucket[]
}

export interface InboxRow {
  email_id: string
  received_at: string
  subject: string
  sender_address: string
  notification_type: string
  triage_category: string
  reason: string
  proposed_folder: string
  keep_in_inbox: boolean
  organized_folder?: string | null
  moved_at?: string | null
}

export interface InboxPreview {
  rows: InboxRow[]
  pending_move: number
  kept_in_inbox: number
  already_filed: number
  by_folder: Record<string, number>
}

export interface VendorTemplate {
  subject: string
  body: string
  schedule_hours: number
  enabled: boolean
  updated_at?: string | null
  updated_by?: string | null
  placeholders: string[]
}

export interface VendorSendResult {
  sent_to: string
  subject: string
  rendered_body: string
  sent_at: string
  status: string
  po_number?: string | null
}

export interface DecisionResponse {
  match_id: number
  extracted_record_id: number
  decision: string
  review_status: ReviewStatus
  extracted_status: string
  decided_by: string
  decided_at: string
  reason: string
  po_line_id?: number | null
  receipt_id?: number | null
  already_applied: boolean
}
