import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../../components/Layout';
import { SeverityBadge, StatusBadge } from '../../components/Badges';
import { incidentsApi, investigationsApi, queryKeys, servicesApi } from '../../api/endpoints';
import { describeError } from '../../api/client';
import { useAuthStore } from '../auth/authStore';
import { CreateIncidentDialog } from './CreateIncidentDialog';
import { formatRelative } from '../../lib/time';

export function IncidentsPage() {
  const [activeOnly, setActiveOnly] = useState(true);
  const [serviceId, setServiceId] = useState<string | undefined>(undefined);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);

  const filters = { activeOnly, serviceId };

  /**
   * Starting a run answers 202 and a row, not a conclusion, so the only sensible thing to do
   * with the answer is go and watch it. The incident list is invalidated because the incident's
   * own status moves to `investigating` as a side effect of the same call.
   */
  const investigate = useMutation({
    mutationFn: (incidentId: string) => investigationsApi.start(incidentId),
    onSuccess: (investigation) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.incidents.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigations.all });
      void navigate(`/investigations/${investigation.id}`);
    },
  });

  const canInvestigate = role === 'engineer' || role === 'admin';

  const services = useQuery({
    queryKey: queryKeys.services.all,
    queryFn: servicesApi.list,
  });

  const incidents = useQuery({
    queryKey: queryKeys.incidents.list(filters),
    queryFn: () => incidentsApi.list(filters),
  });

  return (
    <>
      <PageHeader
        title="Incidents"
        description="Everything the platform is tracking. An incident is what an investigation runs against."
        actions={
          <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
            New incident
          </button>
        }
      />

      <div className="space-y-4 p-8">
        <div className="flex items-center gap-3">
          <select
            className="field w-auto"
            value={serviceId ?? ''}
            onChange={(e) => setServiceId(e.target.value || undefined)}
          >
            <option value="">All services</option>
            {services.data?.map((service) => (
              <option key={service.id} value={service.id}>
                {service.display_name}
              </option>
            ))}
          </select>

          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={activeOnly}
              onChange={(e) => setActiveOnly(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-850 accent-accent"
            />
            Active only
          </label>
        </div>

        {incidents.isPending && <p className="text-sm text-slate-500">Loading incidents...</p>}

        {incidents.error && (
          <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            {describeError(incidents.error)}
          </p>
        )}

        {investigate.error && (
          <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            {describeError(investigate.error)}
          </p>
        )}

        {incidents.data && (
          <div className="panel overflow-hidden">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-ink-800 bg-ink-850">
                <tr className="text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-4 py-3 font-medium">Code</th>
                  <th className="px-4 py-3 font-medium">Title</th>
                  <th className="px-4 py-3 font-medium">Service</th>
                  <th className="px-4 py-3 font-medium">Severity</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Started</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {incidents.data.map((incident) => (
                  <tr key={incident.id} className="hover:bg-ink-850/50">
                    <td className="px-4 py-3">
                      <span className="identifier text-accent">{incident.incident_code}</span>
                    </td>
                    <td className="px-4 py-3 text-slate-200">{incident.title}</td>
                    <td className="px-4 py-3">
                      <span className="identifier text-slate-400">{incident.service_name}</span>
                    </td>
                    <td className="px-4 py-3">
                      <SeverityBadge severity={incident.severity} />
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={incident.status} />
                    </td>
                    <td className="px-4 py-3 text-slate-500">
                      {formatRelative(incident.started_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      {incident.investigation_count > 0 && (
                        <span className="mr-3 text-[11px] text-slate-600">
                          {incident.investigation_count} run
                          {incident.investigation_count === 1 ? '' : 's'}
                        </span>
                      )}
                      {canInvestigate && (
                        <button
                          type="button"
                          className="btn-ghost px-2 py-1 text-xs"
                          disabled={investigate.isPending}
                          onClick={() => investigate.mutate(incident.id)}
                        >
                          {investigate.isPending && investigate.variables === incident.id
                            ? 'Starting…'
                            : 'Investigate'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {incidents.data.length === 0 && (
              <p className="px-4 py-8 text-center text-sm text-slate-500">
                {activeOnly
                  ? 'No active incidents. Everything is quiet.'
                  : 'No incidents recorded yet.'}
              </p>
            )}
          </div>
        )}
      </div>

      {creating && <CreateIncidentDialog onClose={() => setCreating(false)} />}
    </>
  );
}
