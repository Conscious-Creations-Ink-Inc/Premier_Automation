import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  FileSearch,
  FolderInput,
  LayoutDashboard,
  Mail,
  Scale,
  Truck,
} from 'lucide-react'
import { api, queryKeys } from '@/lib/api'
import { cn } from '@/lib/utils'
import Dashboard from '@/pages/Dashboard'
import ExceptionQueue from '@/pages/ExceptionQueue'
import Reconciliation from '@/pages/Reconciliation'
import Extracted from '@/pages/Extracted'
import DeliveryReportPage from '@/pages/DeliveryReport'
import InboxOrganizer from '@/pages/InboxOrganizer'
import VendorTemplatePage from '@/pages/VendorTemplate'

const NAV = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/queue', label: 'Exception queue', icon: AlertTriangle, badge: true },
  { to: '/reconciliation', label: 'All reconciliation', icon: Scale },
  { to: '/extracted', label: 'Extracted records', icon: FileSearch },
  { to: '/delivery', label: 'Delivery report', icon: Truck },
  { to: '/inbox', label: 'Inbox organizer', icon: FolderInput },
  { to: '/vendor', label: 'Vendor template', icon: Mail },
]

export default function App() {
  // Drives the queue badge in the nav; every decision invalidates this key so it stays live.
  const { data: summary } = useQuery({ queryKey: queryKeys.summary, queryFn: api.summary })
  const queueSize = summary?.exception_queue_size ?? 0

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="mx-auto flex max-w-[1500px] gap-6 px-4 py-6 lg:px-6">
        <aside className="hidden w-60 shrink-0 lg:block">
          <div className="sticky top-6">
            <div className="px-2 pb-5">
              <p className="font-mono text-[10px] font-semibold tracking-[0.14em] text-teal-700 uppercase dark:text-teal-400">
                Premier Receiver
              </p>
              <h1 className="mt-1 text-lg leading-tight font-semibold">Reconciliation</h1>
            </div>

            <nav className="flex flex-col gap-0.5">
              {NAV.map(({ to, label, icon: Icon, badge }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors',
                      isActive
                        ? 'bg-white font-medium text-teal-800 shadow-sm ring-1 ring-slate-200 dark:bg-slate-900 dark:text-teal-300 dark:ring-slate-800'
                        : 'text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-900',
                    )
                  }
                >
                  <Icon size={16} className="shrink-0" />
                  <span className="flex-1">{label}</span>
                  {badge && queueSize > 0 && (
                    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                      {queueSize}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>

            <p className="mt-6 px-3 text-[11px] leading-relaxed text-slate-400 dark:text-slate-600">
              Synthetic demo data. Stages 4–7 are simulated; no live mailbox or ERP is connected.
            </p>
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/queue" element={<ExceptionQueue />} />
            <Route path="/reconciliation" element={<Reconciliation />} />
            <Route path="/extracted" element={<Extracted />} />
            <Route path="/delivery" element={<DeliveryReportPage />} />
            <Route path="/inbox" element={<InboxOrganizer />} />
            <Route path="/vendor" element={<VendorTemplatePage />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
