import { useEffect, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

/* Small owned primitives rather than a component library: the same shadcn philosophy — the
   components live in the repo so they can be restyled for a client — without the CLI. */

export type Tone = 'slate' | 'teal' | 'green' | 'amber' | 'red'

const TONE_CLASSES: Record<Tone, string> = {
  slate: 'bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
  teal: 'bg-teal-50 text-teal-700 ring-teal-200 dark:bg-teal-950 dark:text-teal-300 dark:ring-teal-800',
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800',
  amber: 'bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-800',
  red: 'bg-rose-50 text-rose-700 ring-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:ring-rose-800',
}

export function Badge({
  tone = 'slate',
  className,
  children,
}: {
  tone?: Tone
  className?: string
  children: ReactNode
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap',
        TONE_CLASSES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

type ButtonVariant = 'primary' | 'approve' | 'danger' | 'ghost' | 'subtle'

const BUTTON_CLASSES: Record<ButtonVariant, string> = {
  primary: 'bg-teal-700 text-white hover:bg-teal-800 disabled:bg-teal-700/50',
  approve: 'bg-emerald-600 text-white hover:bg-emerald-700 disabled:bg-emerald-600/40',
  danger:
    'bg-white text-rose-700 ring-1 ring-rose-300 hover:bg-rose-50 disabled:opacity-50 dark:bg-transparent dark:text-rose-300 dark:ring-rose-800 dark:hover:bg-rose-950',
  ghost:
    'bg-transparent text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800',
  subtle:
    'bg-white text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-200 dark:ring-slate-700 dark:hover:bg-slate-800',
}

export function Button({
  variant = 'subtle',
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      {...props}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium',
        'transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600',
        'disabled:cursor-not-allowed',
        BUTTON_CLASSES[variant],
        className,
      )}
    />
  )
}

export function Card({
  title,
  subtitle,
  action,
  className,
  bodyClassName,
  children,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  className?: string
  bodyClassName?: string
  children: ReactNode
}) {
  return (
    <section
      className={cn(
        'rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900',
        className,
      )}
    >
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <div>
            {title && <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  footer,
  children,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  footer?: ReactNode
  children: ReactNode
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-[1px]"
        onClick={onClose}
        aria-hidden
      />
      <aside
        role="dialog"
        aria-modal="true"
        className="relative flex h-full w-full max-w-3xl flex-col bg-slate-50 shadow-2xl dark:bg-slate-950"
      >
        <header className="flex items-start justify-between gap-4 border-b border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
            {subtitle && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{subtitle}</div>}
          </div>
          <Button variant="ghost" onClick={onClose} aria-label="Close" className="shrink-0 px-2">
            <X size={18} />
          </Button>
        </header>

        <div className="flex-1 overflow-y-auto p-5">{children}</div>

        {footer && (
          <footer className="border-t border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900">
            {footer}
          </footer>
        )}
      </aside>
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">{hint}</span>}
    </label>
  )
}

const INPUT_CLASSES =
  'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 ' +
  'placeholder:text-slate-400 focus:border-teal-600 focus:outline-none ' +
  'dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100'

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(INPUT_CLASSES, props.className)} />
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(INPUT_CLASSES, 'font-mono text-xs', props.className)} />
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 py-10 text-center dark:border-slate-700">
      <p className="text-sm font-medium text-slate-700 dark:text-slate-300">{title}</p>
      {hint && <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <p className="py-10 text-center text-sm text-slate-500 dark:text-slate-400">{label}</p>
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 ring-1 ring-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:ring-rose-900">
      {message}
    </div>
  )
}
