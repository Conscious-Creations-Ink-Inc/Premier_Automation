import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Mail, X } from 'lucide-react'
import { api, queryKeys, type ApprovePayload } from '@/lib/api'
import { Badge, Button, Card, Drawer, ErrorNote, Field, Input, Loading } from '@/components/ui'
import { ConfidenceBadge, MissingFieldChips, SignalPips } from '@/components/badges'
import { formatQty, humanizeField } from '@/lib/utils'
import type { Candidate, VendorSendResult } from '@/types'

/** Missing-field inputs are typed to what the field actually is. */
const INPUT_TYPE: Record<string, string> = {
  quantity_received: 'number',
  pod_stated_date: 'date',
}

function DetailRow({ label, value, missing }: { label: string; value?: string | null; missing?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-dashed border-slate-200 py-1.5 last:border-0 dark:border-slate-800">
      <span className="font-mono text-[11px] text-slate-500 dark:text-slate-400">{label}</span>
      <span
        className={
          missing
            ? 'font-mono text-xs font-medium text-amber-700 dark:text-amber-400'
            : 'text-right font-mono text-xs text-slate-900 dark:text-slate-100'
        }
      >
        {missing ? '— missing' : (value ?? '—')}
      </span>
    </div>
  )
}

function CandidateCard({
  candidate,
  selected,
  onSelect,
}: {
  candidate: Candidate
  selected: boolean
  onSelect: () => void
}) {
  const line = candidate.po_line
  return (
    <button
      type="button"
      onClick={onSelect}
      className={
        'w-full rounded-lg border px-3 py-2.5 text-left transition-colors ' +
        (selected
          ? 'border-teal-600 bg-teal-50/60 ring-1 ring-teal-600 dark:bg-teal-950/40'
          : 'border-slate-200 bg-white hover:border-slate-300 dark:border-slate-700 dark:bg-slate-900')
      }
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono text-xs text-slate-900 dark:text-slate-100">
          PO {line.po_number} · L{line.line_number} · {line.spec_code}
        </span>
        <SignalPips match={candidate} />
      </div>
      <p className="mt-1 truncate text-xs text-slate-600 dark:text-slate-400">{line.description}</p>
      <p className="tabular mt-1 font-mono text-[11px] text-slate-500 dark:text-slate-500">
        {line.vendor_name} · ordered {line.qty_ordered} · received {line.qty_received} ·{' '}
        <span className={line.qty_outstanding <= 0 ? 'text-amber-700 dark:text-amber-400' : ''}>
          {line.qty_outstanding} outstanding
        </span>
        {candidate.desc_score > 0 && <> · desc {candidate.desc_score}%</>}
      </p>
    </button>
  )
}

export default function ReviewDrawer({
  matchId,
  onClose,
}: {
  matchId: number | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const open = matchId !== null

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.detail(matchId ?? 0),
    queryFn: () => api.detail(matchId as number),
    enabled: open,
  })

  const [selectedLineId, setSelectedLineId] = useState<number | null>(null)
  const [filled, setFilled] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [cancelReason, setCancelReason] = useState('')
  const [cancelling, setCancelling] = useState(false)
  const [sent, setSent] = useState<VendorSendResult | null>(null)

  // Reset per item so one review never leaks into the next.
  useEffect(() => {
    setSelectedLineId(data?.match.po_line_id ?? null)
    setFilled({})
    setNote('')
    setCancelReason('')
    setCancelling(false)
    setSent(null)
  }, [data?.match.id, data?.match.po_line_id])

  const editableMissing = useMemo(
    () => (data?.match.missing_fields ?? []).filter((f) => data?.editable_fields.includes(f)),
    [data],
  )

  const unfilled = editableMissing.filter((field) => !filled[field]?.trim())
  const canApprove = selectedLineId !== null && unfilled.length === 0

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.summary })
    queryClient.invalidateQueries({ queryKey: queryKeys.reconciliation })
    queryClient.invalidateQueries({ queryKey: ['extracted'] })
  }

  const approve = useMutation({
    mutationFn: () => {
      const payload: ApprovePayload = { po_line_id: selectedLineId, note }
      if (editableMissing.length) {
        payload.filled_fields = Object.fromEntries(
          Object.entries(filled).map(([key, value]) => [
            key,
            INPUT_TYPE[key] === 'number' ? Number(value) : value,
          ]),
        )
      }
      return api.approve(matchId as number, payload)
    },
    onSuccess: () => {
      invalidate()
      onClose()
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.cancel(matchId as number, cancelReason),
    onSuccess: () => {
      invalidate()
      onClose()
    },
  })

  const requestInfo = useMutation({
    mutationFn: () => api.requestInfo(matchId as number),
    onSuccess: setSent,
  })

  const record = data?.record.record
  const settled = data && data.match.review_status !== 'pending_review'

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={
        data ? (
          <span className="flex items-center gap-2">
            PO {record?.po_number ?? '—'}
            <span className="font-mono text-sm text-slate-500">{record?.spec_code ?? 'no spec'}</span>
          </span>
        ) : (
          'Review'
        )
      }
      subtitle={
        data && (
          <span className="flex flex-wrap items-center gap-2">
            <ConfidenceBadge confidence={data.match.confidence} signals={data.match.signals_matched} />
            <SignalPips match={data.match} />
            <span className="text-slate-500">{data.match.flag_reason}</span>
          </span>
        )
      }
      footer={
        data && !settled ? (
          cancelling ? (
            <div className="flex flex-col gap-2">
              <Field label="Why is this being cancelled?" hint="Required — it is written to the record.">
                <Input
                  autoFocus
                  value={cancelReason}
                  onChange={(event) => setCancelReason(event.target.value)}
                  placeholder="e.g. duplicate of an earlier receipt"
                />
              </Field>
              {cancel.error && <ErrorNote message={(cancel.error as Error).message} />}
              <div className="flex gap-2">
                <Button
                  variant="danger"
                  disabled={!cancelReason.trim() || cancel.isPending}
                  onClick={() => cancel.mutate()}
                >
                  <X size={15} /> {cancel.isPending ? 'Cancelling…' : 'Confirm cancel'}
                </Button>
                <Button variant="ghost" onClick={() => setCancelling(false)}>
                  Back
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {approve.error && <ErrorNote message={(approve.error as Error).message} />}
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="approve" disabled={!canApprove || approve.isPending} onClick={() => approve.mutate()}>
                  <Check size={15} /> {approve.isPending ? 'Approving…' : 'Approve'}
                </Button>
                <Button variant="danger" onClick={() => setCancelling(true)}>
                  <X size={15} /> Cancel
                </Button>
                <Button variant="ghost" onClick={() => requestInfo.mutate()} disabled={requestInfo.isPending}>
                  <Mail size={15} /> Request info from vendor
                </Button>
                {!canApprove && (
                  <span className="text-xs text-amber-700 dark:text-amber-400">
                    {selectedLineId === null
                      ? 'Choose a PO line to enable approval.'
                      : `Fill ${unfilled.map(humanizeField).join(', ')} to enable approval.`}
                  </span>
                )}
              </div>
            </div>
          )
        ) : null
      }
    >
      {isLoading && <Loading label="Loading item…" />}
      {error && <ErrorNote message={(error as Error).message} />}

      {data && record && (
        <div className="flex flex-col gap-4">
          {settled && (
            <div className="rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              This item is already {data.match.review_status.replace(/_/g, ' ')} — no further action.
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="What we extracted" subtitle={`read from ${record.extraction_source}`}>
              <DetailRow label="PO number" value={record.po_number} missing={!record.po_number} />
              <DetailRow label="Spec ID" value={record.spec_code} missing={!record.spec_code} />
              <DetailRow label="Description" value={record.item_description} missing={!record.item_description} />
              <DetailRow
                label="Quantity"
                value={formatQty(record.quantity_received)}
                missing={record.quantity_received === null || record.quantity_received === undefined}
              />
              <DetailRow label="POD date" value={record.pod_stated_date} missing={!record.pod_stated_date} />
              <DetailRow label="Vendor" value={record.vendor_name} />
              <DetailRow label="Carrier" value={record.carrier_name} />
              <DetailRow label="Tracking" value={record.tracking_number} />

              <div className="mt-3 rounded-md bg-amber-50 px-2.5 py-2 text-xs text-amber-800 ring-1 ring-amber-200 dark:bg-amber-950/60 dark:text-amber-300 dark:ring-amber-900">
                <span className="flex items-start gap-1.5">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                  <span>{data.match.flag_reason}</span>
                </span>
              </div>

              {record.raw_snippet && (
                <div className="mt-3">
                  <p className="mb-1 font-mono text-[10px] tracking-wider text-slate-500 uppercase">
                    Evidence
                  </p>
                  <pre className="overflow-x-auto rounded-md bg-slate-900 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-slate-200">
                    {record.raw_snippet}
                  </pre>
                </div>
              )}
            </Card>

            <div className="flex flex-col gap-4">
              <Card
                title="Candidate PO lines"
                subtitle={
                  data.match.po_line_id === null
                    ? 'Nothing matched automatically — pick the correct line'
                    : 'Pick the correct line'
                }
                bodyClassName="p-3 flex flex-col gap-2 max-h-[22rem] overflow-y-auto"
              >
                {data.candidates.map((candidate) => (
                  <CandidateCard
                    key={candidate.po_line.id}
                    candidate={candidate}
                    selected={selectedLineId === candidate.po_line.id}
                    onSelect={() => setSelectedLineId(candidate.po_line.id)}
                  />
                ))}
              </Card>

              {!settled && editableMissing.length > 0 && (
                <Card title="Fill what the vendor left out" subtitle="Approving writes these onto the record">
                  <div className="flex flex-col gap-3">
                    {editableMissing.map((field) => (
                      <Field key={field} label={humanizeField(field)}>
                        <Input
                          type={INPUT_TYPE[field] ?? 'text'}
                          value={filled[field] ?? ''}
                          onChange={(event) =>
                            setFilled((prev) => ({ ...prev, [field]: event.target.value }))
                          }
                          placeholder={`Enter ${humanizeField(field)}`}
                        />
                      </Field>
                    ))}
                    <Field label="Note (optional)">
                      <Input
                        value={note}
                        onChange={(event) => setNote(event.target.value)}
                        placeholder="e.g. confirmed by phone with the vendor"
                      />
                    </Field>
                  </div>
                </Card>
              )}

              {sent && (
                <Card title="Chase email prepared" subtitle={`to ${sent.sent_to} · ${sent.status}`}>
                  <p className="mb-2 font-mono text-xs text-slate-700 dark:text-slate-300">{sent.subject}</p>
                  <pre className="max-h-40 overflow-y-auto rounded-md bg-slate-50 p-2.5 font-mono text-[11px] whitespace-pre-wrap text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                    {sent.rendered_body}
                  </pre>
                </Card>
              )}
            </div>
          </div>

          {data.decisions.length > 0 && (
            <Card title="Decision history">
              <ul className="flex flex-col gap-2">
                {data.decisions.map((decision) => (
                  <li key={decision.id} className="flex flex-wrap items-center gap-2 text-xs">
                    <Badge tone={decision.decision === 'approve' ? 'green' : 'red'}>{decision.decision}</Badge>
                    <span className="text-slate-600 dark:text-slate-400">
                      by {decision.decided_by} · {new Date(decision.decided_at).toLocaleString()}
                    </span>
                    {decision.reason && <span className="text-slate-500">“{decision.reason}”</span>}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <div className="text-xs text-slate-500 dark:text-slate-400">
            Missing fields: <MissingFieldChips fields={data.match.missing_fields} />
          </div>
        </div>
      )}
    </Drawer>
  )
}
