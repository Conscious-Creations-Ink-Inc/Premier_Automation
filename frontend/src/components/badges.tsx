import { Badge, type Tone } from '@/components/ui'
import { humanizeField } from '@/lib/utils'
import type { Confidence, Match, ReviewStatus } from '@/types'

/* Tones are keyed off the exact strings the backend emits, so the vocabulary can never drift
   between the pipeline and the UI. */

const CONFIDENCE_TONE: Record<Confidence, Tone> = {
  high: 'green',
  medium: 'amber',
  low: 'amber',
  none: 'red',
}

export function ConfidenceBadge({ confidence, signals }: { confidence: Confidence; signals?: number }) {
  return (
    <Badge tone={CONFIDENCE_TONE[confidence]}>
      {confidence}
      {signals !== undefined && <span className="opacity-70">· {signals}/3</span>}
    </Badge>
  )
}

const REVIEW_TONE: Record<ReviewStatus, Tone> = {
  auto_approved: 'teal',
  pending_review: 'amber',
  approved: 'green',
  cancelled: 'red',
}

const REVIEW_LABEL: Record<ReviewStatus, string> = {
  auto_approved: 'auto-approved',
  pending_review: 'needs review',
  approved: 'approved',
  cancelled: 'cancelled',
}

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  return <Badge tone={REVIEW_TONE[status]}>{REVIEW_LABEL[status]}</Badge>
}

const RECORD_TONE: Record<string, Tone> = {
  pending: 'slate',
  matched: 'green',
  failed: 'red',
  routed: 'amber',
}

export function RecordStatusBadge({ status }: { status: string }) {
  return <Badge tone={RECORD_TONE[status] ?? 'slate'}>{status}</Badge>
}

/** The three reconciliation signals at a glance — which ones actually landed. */
export function SignalPips({ match }: { match: Pick<Match, 'po_signal' | 'spec_signal' | 'desc_signal'> }) {
  const signals: Array<[string, boolean]> = [
    ['PO', match.po_signal],
    ['spec', match.spec_signal],
    ['desc', match.desc_signal],
  ]
  return (
    <span className="inline-flex gap-1">
      {signals.map(([label, on]) => (
        <span
          key={label}
          title={on ? `${label} matched` : `${label} did not match`}
          className={
            on
              ? 'rounded bg-emerald-100 px-1.5 py-0.5 font-mono text-[10px] text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
              : 'rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-400 line-through dark:bg-slate-800 dark:text-slate-600'
          }
        >
          {label}
        </span>
      ))}
    </span>
  )
}

export function MissingFieldChips({ fields }: { fields: string[] }) {
  if (!fields.length) return <span className="text-xs text-slate-400">—</span>
  return (
    <span className="inline-flex flex-wrap gap-1">
      {fields.map((field) => (
        <Badge key={field} tone="amber">
          {humanizeField(field)}
        </Badge>
      ))}
    </span>
  )
}
