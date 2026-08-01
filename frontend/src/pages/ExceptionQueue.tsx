import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { Button, Card, EmptyState, ErrorNote, Loading } from '@/components/ui'
import { ConfidenceBadge, MissingFieldChips, SignalPips } from '@/components/badges'
import ReviewDrawer from '@/features/reconciliation/ReviewDrawer'
import { formatQty } from '@/lib/utils'
import type { ReconciliationRow } from '@/types'

export const TH = 'px-3 py-2 text-left font-medium text-slate-500 dark:text-slate-400'
export const TD = 'px-3 py-2.5 align-top'

/** An item can be approved straight from the row only when nothing is left to resolve. */
function isReadyToApprove(row: ReconciliationRow) {
  return row.match.po_line_id !== null && row.match.missing_fields.length === 0
}

export default function ExceptionQueue() {
  const queryClient = useQueryClient()

  // Held in the URL rather than component state so a reviewer can link a colleague straight to
  // the item in question.
  const [searchParams, setSearchParams] = useSearchParams()
  const itemParam = searchParams.get('item')
  const openId = itemParam ? Number(itemParam) : null

  const setOpenId = (id: number | null) => {
    const next = new URLSearchParams(searchParams)
    if (id === null) next.delete('item')
    else next.set('item', String(id))
    setSearchParams(next, { replace: true })
  }

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.exceptions,
    queryFn: api.exceptions,
  })

  const quickApprove = useMutation({
    mutationFn: (matchId: number) => api.approve(matchId, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.summary })
      queryClient.invalidateQueries({ queryKey: queryKeys.reconciliation })
      queryClient.invalidateQueries({ queryKey: ['extracted'] })
    },
  })

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-amber-700 uppercase dark:text-amber-400">
          Needs a person
        </p>
        <h1 className="mt-1 text-2xl font-semibold">Exception queue</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Records the automation could not settle. Open one to see why, pick the right PO line,
          fill whatever the vendor left out, then approve or cancel.
        </p>
      </header>

      {isLoading && <Loading label="Loading queue…" />}
      {error && <ErrorNote message={(error as Error).message} />}
      {quickApprove.error && <ErrorNote message={(quickApprove.error as Error).message} />}

      {data && data.length === 0 && (
        <EmptyState
          title="Nothing is waiting"
          hint="Every extracted record has been resolved. Reset the demo data to review again."
        />
      )}

      {data && data.length > 0 && (
        <Card bodyClassName="p-0">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[64rem] text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs dark:border-slate-800 dark:bg-slate-800/50">
                <tr>
                  <th className={TH}>PO / spec</th>
                  <th className={TH}>Description</th>
                  <th className={TH}>Qty</th>
                  <th className={TH}>Confidence</th>
                  <th className={TH}>Missing</th>
                  <th className={TH}>Why it stalled</th>
                  <th className={`${TH} text-right`}>Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {data.map((row) => {
                  const record = row.record.record
                  const ready = isReadyToApprove(row)
                  return (
                    <tr key={row.match.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                      <td className={TD}>
                        <div className="font-mono text-xs font-medium">{record.po_number ?? '—'}</div>
                        <div className="font-mono text-[11px] text-slate-500">
                          {record.spec_code ?? 'no spec'}
                        </div>
                      </td>
                      <td className={`${TD} max-w-[16rem]`}>
                        <div className="truncate text-xs text-slate-700 dark:text-slate-300">
                          {record.item_description ?? '—'}
                        </div>
                        <div className="mt-1">
                          <SignalPips match={row.match} />
                        </div>
                      </td>
                      <td className={`${TD} tabular font-mono text-xs`}>
                        {formatQty(record.quantity_received)}
                      </td>
                      <td className={TD}>
                        <ConfidenceBadge
                          confidence={row.match.confidence}
                          signals={row.match.signals_matched}
                        />
                      </td>
                      <td className={TD}>
                        <MissingFieldChips fields={row.match.missing_fields} />
                      </td>
                      <td className={`${TD} max-w-[20rem] text-xs text-slate-600 dark:text-slate-400`}>
                        {row.match.flag_reason}
                      </td>
                      <td className={`${TD} text-right whitespace-nowrap`}>
                        <div className="inline-flex gap-1.5">
                          {ready && (
                            <Button
                              variant="approve"
                              className="px-2.5 py-1.5 text-xs"
                              disabled={quickApprove.isPending}
                              onClick={() => quickApprove.mutate(row.match.id)}
                              title="Nothing left to resolve — approve directly"
                            >
                              <Check size={13} /> Approve
                            </Button>
                          )}
                          <Button
                            variant="subtle"
                            className="px-2.5 py-1.5 text-xs"
                            onClick={() => setOpenId(row.match.id)}
                          >
                            Review
                          </Button>
                        </div>
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
