import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { PageHeader } from '../../components/Layout';
import { SeverityBadge, StatusBadge } from '../../components/Badges';
import { incidentsApi, queryKeys, servicesApi } from '../../api/endpoints';
import { formatRelative } from '../../lib/time';

export function DashboardPage() {
  const services = useQuery({
    queryKey: queryKeys.services.all,
    queryFn: servicesApi.list,
  });

  const activeFilters = { activeOnly: true, limit: 10 };
  const active = useQuery({
    queryKey: queryKeys.incidents.list(activeFilters),
    queryFn: () => incidentsApi.list(activeFilters),
  });

  const openCount = active.data?.length ?? 0;
  const criticalCount = active.data?.filter((i) => i.severity === 'critical').length ?? 0;

  return (
    <>
      <PageHeader title="Dashboard" description="Current state of the monitored estate." />

      <div className="space-y-6 p-8">
        <div className="grid grid-cols-3 gap-4">
          <Stat label="Services" value={services.data?.length ?? '-'} />
          <Stat label="Active incidents" value={openCount} emphasis={openCount > 0} />
          <Stat label="Critical" value={criticalCount} emphasis={criticalCount > 0} />
        </div>

        <section className="panel">
          <div className="flex items-center justify-between border-b border-ink-800 px-5 py-3">
            <h2 className="text-sm font-semibold text-slate-200">Active incidents</h2>
            <Link to="/incidents" className="text-xs text-accent hover:underline">
              View all
            </Link>
          </div>

          {active.data?.length === 0 ? (
            <p className="px-5 py-10 text-center text-sm text-slate-500">
              Nothing active. Trigger a chaos scenario on a sample service to create one.
            </p>
          ) : (
            <ul className="divide-y divide-ink-800">
              {active.data?.map((incident) => (
                <li key={incident.id} className="flex items-center gap-4 px-5 py-3">
                  <span className="identifier w-24 shrink-0 text-accent">
                    {incident.incident_code}
                  </span>
                  <span className="flex-1 truncate text-sm text-slate-200">{incident.title}</span>
                  <span className="identifier w-32 shrink-0 text-slate-500">
                    {incident.service_name}
                  </span>
                  <SeverityBadge severity={incident.severity} />
                  <StatusBadge status={incident.status} />
                  <span className="w-20 shrink-0 text-right text-xs text-slate-500">
                    {formatRelative(incident.started_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/*
          This card described Phase 1 until Phase 12 had shipped, which made the front page of a
          finished system promise an investigation agent that was already running behind it.
        */}
        <section className="panel px-5 py-4">
          <h2 className="mb-2 text-sm font-semibold text-slate-200">What this is</h2>
          <p className="text-sm leading-relaxed text-slate-400">
            Five sample microservices that fail in fifteen scripted ways, and an agent that reads
            the logs, metrics, traces, database and prior incidents they produce — through eight
            MCP servers — to work out which failure happened, citing the evidence for each step.
            It is measured rather than asserted: every benchmark it is judged on is under
            Evaluation, including the ones it does poorly at. A conclusion the evidence does not
            support is left for a human, and anything that changes the estate needs approval.
          </p>
        </section>
      </div>
    </>
  );
}

function Stat({
  label,
  value,
  emphasis = false,
}: {
  label: string;
  value: number | string;
  emphasis?: boolean;
}) {
  return (
    <div className="panel px-5 py-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div
        className={`mt-1 text-2xl font-semibold ${
          emphasis ? 'text-state-warn' : 'text-slate-100'
        }`}
      >
        {value}
      </div>
    </div>
  );
}
