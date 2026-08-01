import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderInput, Inbox } from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { Badge, Button, Card, ErrorNote, Loading } from '@/components/ui'
import { TD, TH } from '@/pages/ExceptionQueue'
import { formatDate } from '@/lib/utils'

export default function InboxOrganizer() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.inbox,
    queryFn: api.inboxPreview,
  })

  const organize = useMutation({
    mutationFn: () => api.organize(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.inbox }),
  })

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
          Step 4 — tidy the inbox
        </p>
        <h1 className="mt-1 text-2xl font-semibold">Inbox organizer</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          File the repetitive status mail into folders. What stays behind is what someone actually has
          to read: order confirmations, cancellations and anything unclear.
        </p>
      </header>

      {isLoading && <Loading />}
      {error && <ErrorNote message={(error as Error).message} />}
      {organize.error && <ErrorNote message={(organize.error as Error).message} />}

      {data && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex-1 rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
              <p className="font-mono text-[10px] tracking-[0.1em] text-slate-500 uppercase">To file</p>
              <p className="tabular mt-1 text-2xl font-semibold">{data.pending_move}</p>
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                {Object.entries(data.by_folder)
                  .map(([folder, count]) => `${count} → ${folder}`)
                  .join(' · ') || 'nothing pending'}
              </p>
            </div>
            <div className="flex-1 rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
              <p className="font-mono text-[10px] tracking-[0.1em] text-slate-500 uppercase">
                Stays in inbox
              </p>
              <p className="tabular mt-1 text-2xl font-semibold">{data.kept_in_inbox}</p>
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                confirmations, cancellations, unclear
              </p>
            </div>
            <div className="flex-1 rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
              <p className="font-mono text-[10px] tracking-[0.1em] text-slate-500 uppercase">
                Already filed
              </p>
              <p className="tabular mt-1 text-2xl font-semibold">{data.already_filed}</p>
            </div>
          </div>

          <div>
            <Button
              variant="primary"
              disabled={data.pending_move === 0 || organize.isPending}
              onClick={() => organize.mutate()}
            >
              <FolderInput size={15} />
              {organize.isPending
                ? 'Filing…'
                : data.pending_move === 0
                  ? 'Nothing left to file'
                  : `File ${data.pending_move} status mails`}
            </Button>
            <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
              Simulated — no live mailbox is connected in this phase.
            </p>
          </div>

          <Card bodyClassName="p-0">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[56rem] text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs dark:border-slate-800 dark:bg-slate-800/50">
                  <tr>
                    <th className={TH}>Received</th>
                    <th className={TH}>Subject</th>
                    <th className={TH}>Type</th>
                    <th className={TH}>Destination</th>
                    <th className={TH}>Why</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {data.rows.map((row) => (
                    <tr key={row.email_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                      <td className={`${TD} font-mono text-xs whitespace-nowrap`}>
                        {formatDate(row.received_at)}
                      </td>
                      <td className={`${TD} max-w-[22rem]`}>
                        <span className="block truncate text-xs text-slate-700 dark:text-slate-300">
                          {row.subject}
                        </span>
                        <span className="font-mono text-[11px] text-slate-400">{row.sender_address}</span>
                      </td>
                      <td className={`${TD} text-xs text-slate-600 dark:text-slate-400`}>
                        {row.notification_type.replace(/_/g, ' ')}
                      </td>
                      <td className={TD}>
                        {row.keep_in_inbox ? (
                          <Badge tone="slate">
                            <Inbox size={11} /> stays in inbox
                          </Badge>
                        ) : row.organized_folder ? (
                          <Badge tone="green">filed → {row.organized_folder}</Badge>
                        ) : (
                          <Badge tone="teal">→ {row.proposed_folder}</Badge>
                        )}
                      </td>
                      <td className={`${TD} max-w-[18rem] text-xs text-slate-500 dark:text-slate-400`}>
                        {row.reason || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
