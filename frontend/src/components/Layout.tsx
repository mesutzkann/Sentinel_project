import type { ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuthStore } from '../features/auth/authStore';

/**
 * Every section of the system, and since Phase 11 every one of them exists.
 *
 * This list used to carry a `phase` on the sections that had not been built, drawn greyed out
 * with the phase that would bring them — the shape of the finished product, visible from the
 * first screen. The last of them (Knowledge Base, Models, Evaluation) landed in Phases 9, 8 and
 * 11, so the mechanism went with them rather than staying as a branch nothing takes.
 */
const navigation = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/incidents', label: 'Incidents' },
  { to: '/services', label: 'Services' },
  { to: '/investigations', label: 'Investigations' },
  { to: '/knowledge', label: 'Knowledge Base' },
  { to: '/mcp', label: 'MCP Tools' },
  { to: '/models', label: 'Models' },
  { to: '/evaluation', label: 'Evaluation' },
];

export function Layout() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-ink-800 bg-ink-900">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="h-7 w-7 rounded bg-accent/20 ring-1 ring-accent/40" aria-hidden>
            <div className="m-[9px] h-2.5 w-2.5 rounded-sm bg-accent" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold text-slate-100">SentinelAI</div>
            <div className="text-[11px] text-slate-500">Incident Intelligence</div>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 px-3">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-accent/10 font-medium text-accent'
                    : 'text-slate-400 hover:bg-ink-800 hover:text-slate-200'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-ink-800 p-3">
          <div className="px-2 pb-2">
            <div className="text-sm text-slate-300">{user?.username}</div>
            <div className="text-[11px] uppercase tracking-wide text-slate-500">{user?.role}</div>
          </div>
          <button type="button" onClick={logout} className="btn-ghost w-full">
            Sign out
          </button>
        </div>
      </aside>

      <main className="flex-1 overflow-x-hidden">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex items-start justify-between border-b border-ink-800 px-8 py-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
        {description && <p className="mt-1 text-sm text-slate-400">{description}</p>}
      </div>
      {actions}
    </header>
  );
}
