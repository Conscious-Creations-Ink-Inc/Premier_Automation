import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Merge conditional class names, with later Tailwind utilities winning conflicts. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Field keys are snake_case on the wire; show them as words. */
export function humanizeField(field: string) {
  const labels: Record<string, string> = {
    po_number: 'PO number',
    spec_code: 'spec ID',
    quantity_received: 'quantity',
    pod_stated_date: 'POD date',
    item_description: 'description',
    unit_of_measure: 'unit',
  }
  return labels[field] ?? field.replace(/_/g, ' ')
}

export function formatDate(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })
}

export function formatQty(value?: number | null) {
  return value === null || value === undefined ? '—' : String(value)
}
