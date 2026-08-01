import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, RotateCcw } from 'lucide-react'
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, queryKeys } from '@/lib/api'
import { Button, Card, ErrorNote, Loading } from '@/components/ui'

const PALETTE = {
  teal: '#0e7490',
  emerald: '#059669',
  amber: '#b45309',
  rose: '#b4241c',
  slate: '#64748b',
}

const CONFIDENCE_COLOR: Record<string, string> = {
  high: PALETTE.emerald,
  medium: PALETTE.amber,
  low: PALETTE.amber,
  none: PALETTE.rose,
}

function Tile({
  label,
  value,
  hint,
  accent = 'text-slate-900 dark:text-slate-100',
}: {
  label: string
  value: number | string
  hint?: string
  accent?: string
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <p className="font-mono text-[10px] tracking-[0.1em] text-slate-500 uppercase dark:text-slate-400">
        {label}
      </p>
      <p className={`tabular mt-1.5 text-2xl font-semibold ${accent}`}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  )
}

export default function Dashboard() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: queryKeys.summary, queryFn: api.summary })

  const reset = useMutation({
    mutationFn: api.resetDemo,
    onSuccess: () => queryClient.invalidateQueries(),
  })

  if (isLoading) return <Loading label="Loading dashboard…" />
  if (error) return <ErrorNote message={(error as Error).message} />
  if (!data) return null

  const { totals, funnel, by_confidence, by_review_status } = data
  const queue = data.exception_queue_size

  const confidenceData = Object.entries(by_confidence).map(([name, value]) => ({ name, value }))
  const funnelData = funnel.map((stage) => ({ name: stage.label, value: stage.count }))

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
            Automation progress
          </p>
          <h1 className="mt-1 text-2xl font-semibold">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Most delivery mail reconciles itself. What lands in the queue is what could not.
          </p>
        </div>
        <Button variant="subtle" onClick={() => reset.mutate()} disabled={reset.isPending}>
          <RotateCcw size={15} />
          {reset.isPending ? 'Resetting…' : 'Reset demo data'}
        </Button>
      </header>

      {/* The queue is the reason this dashboard exists, so it leads. */}
      <Link
        to="/queue"
        className="group flex items-center justify-between rounded-xl border border-amber-300 bg-amber-50 px-5 py-4 transition-colors hover:bg-amber-100 dark:border-amber-900 dark:bg-amber-950/50 dark:hover:bg-amber-950"
      >
        <div>
          <p className="font-mono text-[10px] tracking-[0.1em] text-amber-800 uppercase dark:text-amber-400">
            Needs a person
          </p>
          <p className="tabular mt-1 text-3xl font-semibold text-amber-900 dark:text-amber-200">
            {queue}
          </p>
          <p className="mt-0.5 text-sm text-amber-800 dark:text-amber-300">
            {queue === 0
              ? 'Nothing is waiting — every record has been resolved.'
              : 'Flagged for low confidence or missing information.'}
          </p>
        </div>
        <span className="flex items-center gap-1.5 text-sm font-medium text-amber-900 group-hover:gap-2.5 dark:text-amber-200">
          Review <ArrowRight size={16} className="transition-all" />
        </span>
      </Link>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Tile label="Auto-approved" value={totals.auto_approved} hint="settled with no human" />
        <Tile label="Approved" value={totals.approved} hint="resolved by a reviewer" />
        <Tile label="Cancelled" value={totals.cancelled} hint="rejected with a reason" />
        <Tile label="Receipts staged" value={totals.staged_receipts} hint="ready for the ERP" />
        <Tile label="Records extracted" value={totals.extracted_records} hint={`from ${totals.emails} mails`} />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Pipeline funnel" subtitle="Where the volume goes, stage by stage">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={funnelData} layout="vertical" margin={{ left: 8, right: 16 }}>
              <XAxis type="number" tick={{ fontSize: 11, fill: PALETTE.slate }} axisLine={false} tickLine={false} />
              <YAxis
                type="category"
                dataKey="name"
                width={116}
                tick={{ fontSize: 11, fill: PALETTE.slate }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                cursor={{ fill: 'rgba(100,116,139,0.08)' }}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }}
              />
              <Bar
                dataKey="value"
                fill={PALETTE.teal}
                radius={[0, 4, 4, 0]}
                barSize={16}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        <Card title="Match confidence" subtitle="How certain reconciliation was, across all records">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={confidenceData} margin={{ left: 0, right: 8 }}>
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: PALETTE.slate }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: PALETTE.slate }} axisLine={false} tickLine={false} allowDecimals={false} />
              <Tooltip
                cursor={{ fill: 'rgba(100,116,139,0.08)' }}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }}
              />
              <Bar dataKey="value" radius={[4, 4, 0, 0]} barSize={44} isAnimationActive={false}>
                {confidenceData.map((entry) => (
                  <Cell key={entry.name} fill={CONFIDENCE_COLOR[entry.name] ?? PALETTE.slate} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Card>
      </div>

      <Card title="Review outcomes" subtitle="Every record ends in exactly one of these">
        <div className="grid gap-3 sm:grid-cols-4">
          {Object.entries(by_review_status).map(([status, count]) => (
            <div key={status} className="rounded-lg bg-slate-50 p-3 dark:bg-slate-800/50">
              <p className="text-xs text-slate-500 dark:text-slate-400">{status.replace(/_/g, ' ')}</p>
              <p className="tabular mt-1 text-xl font-semibold">{count}</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
