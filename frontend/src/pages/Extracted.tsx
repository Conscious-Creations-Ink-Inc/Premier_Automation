import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Paperclip, Search } from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { Card, EmptyState, ErrorNote, Input, Loading } from '@/components/ui'
import { RecordStatusBadge } from '@/components/badges'
import { TD, TH } from '@/pages/ExceptionQueue'
import { formatDate, formatQty } from '@/lib/utils'

const SELECT =
  'rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900'

export default function Extracted() {
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')

  const params = { status: status || undefined, q: search || undefined }
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.extracted(params),
    queryFn: () => api.extracted(params),
  })

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
          Step 1 — parse &amp; extract
        </p>
        <h1 className="mt-1 text-2xl font-semibold">Extracted records</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          What the parser pulled out of each mail body and attachment. Every value traces back to the
          evidence it was read from.
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search size={14} className="absolute top-2.5 left-2.5 text-slate-400" />
          <Input
            className="w-64 pl-8"
            placeholder="Search PO, spec or description"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <select className={SELECT} value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">Any status</option>
          <option value="pending">Pending</option>
          <option value="matched">Matched</option>
          <option value="failed">Failed</option>
        </select>
        {data && <span className="text-xs text-slate-500 dark:text-slate-400">{data.length} records</span>}
      </div>

      {isLoading && <Loading />}
      {error && <ErrorNote message={(error as Error).message} />}
      {data && data.length === 0 && <EmptyState title="No records match" />}

      {data && data.length > 0 && (
        <Card bodyClassName="p-0">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[68rem] text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs dark:border-slate-800 dark:bg-slate-800/50">
                <tr>
                  <th className={TH}>PO</th>
                  <th className={TH}>Spec</th>
                  <th className={TH}>Description</th>
                  <th className={TH}>Qty</th>
                  <th className={TH}>POD date</th>
                  <th className={TH}>Vendor / carrier</th>
                  <th className={TH}>Read from</th>
                  <th className={TH}>Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {data.map(({ record: row, email }) => {
                  const record = row.record
                  return (
                    <tr key={row.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                      <td className={`${TD} font-mono text-xs`}>{record.po_number ?? '—'}</td>
                      <td className={`${TD} font-mono text-xs`}>
                        {record.spec_code ?? <span className="text-amber-700">— missing</span>}
                      </td>
                      <td className={`${TD} max-w-[18rem]`}>
                        <span className="block truncate text-xs text-slate-700 dark:text-slate-300">
                          {record.item_description ?? '—'}
                        </span>
                        {record.raw_snippet && (
                          <span className="mt-0.5 block truncate font-mono text-[10px] text-slate-400">
                            {record.raw_snippet}
                          </span>
                        )}
                      </td>
                      <td className={`${TD} tabular font-mono text-xs`}>
                        {record.quantity_received === null || record.quantity_received === undefined ? (
                          <span className="text-amber-700">— missing</span>
                        ) : (
                          formatQty(record.quantity_received)
                        )}
                      </td>
                      <td className={`${TD} font-mono text-xs`}>
                        {record.pod_stated_date ? (
                          formatDate(record.pod_stated_date)
                        ) : (
                          <span className="text-amber-700">— missing</span>
                        )}
                      </td>
                      <td className={`${TD} text-xs text-slate-600 dark:text-slate-400`}>
                        <div>{record.vendor_name ?? '—'}</div>
                        <div className="text-[11px] text-slate-400">{record.carrier_name ?? ''}</div>
                      </td>
                      <td className={TD}>
                        <span className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                          {record.extraction_source}
                          {email?.has_attachment && <Paperclip size={10} />}
                        </span>
                      </td>
                      <td className={TD}>
                        <RecordStatusBadge status={row.status} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
