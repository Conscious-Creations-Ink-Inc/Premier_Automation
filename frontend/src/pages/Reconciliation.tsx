import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '@/lib/api'
import { Button, Card, EmptyState, ErrorNote, Loading } from '@/components/ui'
import { ConfidenceBadge, RecordStatusBadge, ReviewStatusBadge, SignalPips } from '@/components/badges'
import ReviewDrawer from '@/features/reconciliation/ReviewDrawer'
import { TD, TH } from '@/pages/ExceptionQueue'
import { formatQty } from '@/lib/utils'

const SELECT =
  'rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900'

export default function Reconciliation() {
  const [reviewStatus, setReviewStatus] = useState('')
  const [confidence, setConfidence] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)

  const params = { review_status: reviewStatus || undefined, confidence: confidence || undefined }
  const { data, isLoading, error } = useQuery({
    queryKey: [...queryKeys.reconciliation, params],
    queryFn: () => api.reconciliation(params),
  })

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
          Every record
        </p>
        <h1 className="mt-1 text-2xl font-semibold">All reconciliation</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Everything the automation matched, settled or flagged — including what it resolved without
          anyone looking.
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <select className={SELECT} value={reviewStatus} onChange={(e) => setReviewStatus(e.target.value)}>
          <option value="">All outcomes</option>
          <option value="auto_approved">Auto-approved</option>
          <option value="pending_review">Needs review</option>
          <option value="approved">Approved</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <select className={SELECT} value={confidence} onChange={(e) => setConfidence(e.target.value)}>
          <option value="">Any confidence</option>
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
          <option value="none">None</option>
        </select>
        {data && (
          <span className="text-xs text-slate-500 dark:text-slate-400">{data.length} records</span>
        )}
      </div>

      {isLoading && <Loading />}
      {error && <ErrorNote message={(error as Error).message} />}
      {data && data.length === 0 && <EmptyState title="No records match those filters" />}

      {data && data.length > 0 && (
        <Card bodyClassName="p-0">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[60rem] text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs dark:border-slate-800 dark:bg-slate-800/50">
                <tr>
                  <th className={TH}>PO / spec</th>
                  <th className={TH}>Description</th>
                  <th className={TH}>Qty</th>
                  <th className={TH}>Signals</th>
                  <th className={TH}>Confidence</th>
                  <th className={TH}>Outcome</th>
                  <th className={TH}>Record</th>
                  <th className={`${TH} text-right`}>Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {data.map((row) => {
                  const record = row.record.record
                  return (
                    <tr key={row.match.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                      <td className={TD}>
                        <div className="font-mono text-xs font-medium">{record.po_number ?? '—'}</div>
                        <div className="font-mono text-[11px] text-slate-500">
                          {record.spec_code ?? 'no spec'}
                        </div>
                      </td>
                      <td className={`${TD} max-w-[18rem]`}>
                        <span className="block truncate text-xs text-slate-700 dark:text-slate-300">
                          {record.item_description ?? '—'}
                        </span>
                      </td>
                      <td className={`${TD} tabular font-mono text-xs`}>
                        {formatQty(record.quantity_received)}
                      </td>
                      <td className={TD}>
                        <SignalPips match={row.match} />
                      </td>
                      <td className={TD}>
                        <ConfidenceBadge confidence={row.match.confidence} />
                      </td>
                      <td className={TD}>
                        <ReviewStatusBadge status={row.match.review_status} />
                      </td>
                      <td className={TD}>
                        <RecordStatusBadge status={row.record.status} />
                      </td>
                      <td className={`${TD} text-right`}>
                        <Button
                          variant="subtle"
                          className="px-2.5 py-1.5 text-xs"
                          onClick={() => setOpenId(row.match.id)}
                        >
                          Open
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <ReviewDrawer matchId={openId} onClose={() => setOpenId(null)} />
    </div>
  )
}
