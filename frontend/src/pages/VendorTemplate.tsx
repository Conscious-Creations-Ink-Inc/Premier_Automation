import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Save, Send } from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { Badge, Button, Card, ErrorNote, Field, Input, Loading, Textarea } from '@/components/ui'
import type { VendorSendResult } from '@/types'

export default function VendorTemplatePage() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.template,
    queryFn: api.vendorTemplate,
  })

  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [hours, setHours] = useState(24)
  const [enabled, setEnabled] = useState(false)
  const [preview, setPreview] = useState<VendorSendResult | null>(null)

  useEffect(() => {
    if (!data) return
    setSubject(data.subject)
    setBody(data.body)
    setHours(data.schedule_hours)
    setEnabled(data.enabled)
  }, [data])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.template })

  const save = useMutation({
    mutationFn: () => api.saveTemplate({ subject, body }),
    onSuccess: invalidate,
  })
  const schedule = useMutation({
    mutationFn: () => api.setSchedule(hours, enabled),
    onSuccess: invalidate,
  })
  const send = useMutation({
    mutationFn: () => api.sendTemplate(),
    onSuccess: setPreview,
  })

  if (isLoading) return <Loading />
  if (error) return <ErrorNote message={(error as Error).message} />

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="font-mono text-[10px] tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
          Step 5 — ask for what we need
        </p>
        <h1 className="mt-1 text-2xl font-semibold">Vendor template</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          One format we actually accept. Send it to any vendor whose confirmations arrive without the
          fields reconciliation needs.
        </p>
      </header>

      <div className="grid gap-5 lg:grid-cols-3">
        <Card
          className="lg:col-span-2"
          title="Template"
          subtitle="Placeholders are filled in per record when the mail is prepared"
          action={
            <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
              <Save size={15} /> {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          }
        >
          <div className="flex flex-col gap-3">
            <Field label="Subject">
              <Input value={subject} onChange={(event) => setSubject(event.target.value)} />
            </Field>
            <Field label="Body">
              <Textarea
                rows={18}
                value={body}
                onChange={(event) => setBody(event.target.value)}
              />
            </Field>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-slate-500 dark:text-slate-400">Available placeholders:</span>
              {data?.placeholders.map((token) => (
                <Badge key={token} tone="teal">
                  <span className="font-mono">{token}</span>
                </Badge>
              ))}
            </div>
            {save.error && <ErrorNote message={(save.error as Error).message} />}
            {data?.updated_at && (
              <p className="text-xs text-slate-500 dark:text-slate-400">
                Last saved {new Date(data.updated_at).toLocaleString()} by {data.updated_by}
              </p>
            )}
          </div>
        </Card>

        <div className="flex flex-col gap-5">
          <Card title="Automatic sending" subtitle="Chase under-reporting vendors on a cadence">
            <div className="flex flex-col gap-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(event) => setEnabled(event.target.checked)}
                  className="size-4 accent-teal-700"
                />
                Send automatically
              </label>
              <Field label="Every N hours">
                <Input
                  type="number"
                  min={1}
                  max={720}
                  value={hours}
                  onChange={(event) => setHours(Number(event.target.value))}
                />
              </Field>
              <Button variant="subtle" onClick={() => schedule.mutate()} disabled={schedule.isPending}>
                <Clock size={15} /> {schedule.isPending ? 'Saving…' : 'Save schedule'}
              </Button>
              {schedule.data && (
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  {schedule.data.enabled
                    ? `Next run ${new Date(schedule.data.next_run_at as string).toLocaleString()}`
                    : 'Automatic sending is off.'}
                </p>
              )}
              <p className="text-xs text-slate-400">
                Stored only — no scheduler runs and no mail is dispatched in this phase.
              </p>
            </div>
          </Card>

          <Card title="Preview" subtitle="Render the template without sending anything">
            <Button variant="subtle" onClick={() => send.mutate()} disabled={send.isPending}>
              <Send size={15} /> {send.isPending ? 'Rendering…' : 'Preview a message'}
            </Button>
            {preview && (
              <div className="mt-3">
                <p className="font-mono text-xs text-slate-700 dark:text-slate-300">{preview.subject}</p>
                <pre className="mt-2 max-h-72 overflow-y-auto rounded-md bg-slate-50 p-2.5 font-mono text-[11px] whitespace-pre-wrap text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                  {preview.rendered_body}
                </pre>
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}
