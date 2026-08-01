import { useQuery } from '@tanstack/react-query'
import { Paperclip } from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { Badge, Card, ErrorNote, Loading, type Tone } from '@/components/ui'
import { TD, TH } from '@/pages/ExceptionQueue'
import { formatDate } from '@/lib/utils'

/** One tone per bucket, matching the workflow diagram's colour language. */
const BUCKET_TONE: Record<string, Tone> = {
  delivered_received: 'green',
  in_transit: 'amber',
  cancelled: 'red',
}

export default function DeliveryReportPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.delivery,
    queryFn: api.deliveryReport,
  })

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
          Step 2 — the report
        </p>
        <h1 className="mt-1 text-2xl font-semibold">Delivery report</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Every delivery-related mail, bucketed by what it actually said happened — so nothing is lost
          in the noise.
        </p>
      </header>

      {isLoading && <Loading />}
      {error && <ErrorNote message={(error as Error).message} />}

      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            {data.buckets.map((bucket) => (
              <div
                key={bucket.key}
                className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900"
              >
                <Badge tone={BUCKET_TONE[bucket.key] ?? 'slate'}>{bucket.label}</Badge>
                <p className="tabular mt-2 text-2xl font-semibold">{bucket.count}</p>
              </div>
            ))}
          </div>

          {data.buckets.map((bucket) => (
            <Card
              key={bucket.key}
              title={bucket.label}
              subtitle={`${bucket.count} of ${data.total} mails`}
              bodyClassName="p-0"
            >
              {bucket.rows.length === 0 ? (
                <p className="px-4 py-6 text-center text-xs text-slate-400">Nothing in this bucket.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[52rem] text-sm">
                    <thead className="border-b border-slate-200 bg-slate-50 text-xs dark:border-slate-800 dark:bg-slate-800/50">
                      <tr>
                        <th className={TH}>Received</th>
                        <th className={TH}>Subject</th>
                        <th className={TH}>From</th>
                        <th className={TH}>PO</th>
                        <th className={TH}>Type</th>
                        <th className={TH}>Status word</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {bucket.rows.map((row) => (
                        <tr key={row.email_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                          <td className={`${TD} font-mono text-xs whitespace-nowrap`}>
                            {formatDate(row.received_at)}
                          </td>
                          <td className={`${TD} max-w-[22rem]`}>
                            <span className="flex items-center gap-1.5 truncate text-xs text-slate-700 dark:text-slate-300">
                              {row.subject}
                              {row.has_attachment && <Paperclip size={11} className="shrink-0 text-slate-400" />}
                            </span>
                          </td>
                          <td className={`${TD} font-mono text-[11px] text-slate-500`}>{row.sender_address}</td>
                          <td className={`${TD} font-mono text-xs`}>{row.po_number ?? '—'}</td>
                          <td className={`${TD} text-xs text-slate-600 dark:text-slate-400`}>
                            {row.notification_type.replace(/_/g, ' ')}
                          </td>
                          <td className={TD}>
                            <span className="font-mono text-[11px] text-slate-600 dark:text-slate-400">
                              {row.status_keyword.replace(/_/g, ' ')}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          ))}
        </>
      )}
    </div>
  )
}
