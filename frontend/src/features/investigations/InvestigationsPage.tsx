import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { InvestigationStatusBadge } from '../../components/Badges';
import { describeError } from '../../api/client';
import { investigationsApi, queryKeys } from '../../api/endpoints';
import { formatDuration, formatRelative } from '../../lib/time';

/**
 * Every run the agent has made.
 *
 * The two columns that matter are the conclusion and its confidence, side by side: a run that
 * concluded and a run that concluded *and would act on it* are different outcomes, and the
 * number is what separates them.
 */
export function InvestigationsPage() {
  const investigations = useQuery({
    queryKey: queryKeys.investigations.list(),
    queryFn: () => investigationsApi.list(),
    // A running investigation changes on its own; this list is the place people wait on one.
    refetchInterval: (query) =>
      query.state.data?.some((i) => i.status === 'running' || i.status === 'queued') ? 5000 : false,
  });

  return (
    <>
      <PageHeader
        title="Investigations"
        description="What the agent looked at, what it concluded, and how sure it was."
      />

      <div className="p-8">
        {investigations.isPending && (
          <p className="text-sm text-slate-500">Loading investigations…</p>
        )}

        {investigations.error && (
          <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            {describeError(investigations.error)}
          </p>
        )}

        {investigations.data && (
          <div className="panel overflow-hidden">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-ink-800 bg-ink-850">
                <tr className="text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-4 py-3 font-medium">Incident</th>
                  <th className="px-4 py-3 font-medium">Concluded</th>
                  <th className="px-4 py-3 font-medium">Confidence</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Cost</th>
                  <th className="px-4 py-3 font-medium">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {investigations.data.map((investigation) => (
                  <tr key={investigation.id} className="hover:bg-ink-850/50">
                    <td className="px-4 py-3">
                      <Link
                        to={`/investigations/${investigation.id}`}
                        className="identifier text-accent hover:underline"
                      >
                        {investigation.incident_code}
                      </Link>
                    </td>

                    <td className="max-w-md px-4 py-3">
                      {investigation.root_cause_title ? (
                        <>
                          <span className="block truncate text-slate-200">
                            {investigation.root_cause_title}
                          </span>
                          <span className="identifier text-[11px] text-slate-500">
                            {investigation.root_cause_category}
                          </span>
                        </>
                      ) : (
                        <span className="truncate text-slate-500">{investigation.query}</span>
                      )}
                    </td>

                    <td className="px-4 py-3">
                      {investigation.confidence === null ? (
                        <span className="text-slate-600">—</span>
                      ) : (
                        <span
                          className={`font-mono ${
                            investigation.confidence >= 0.7 ? 'text-state-ok' : 'text-state-warn'
                          }`}
                        >
                          {investigation.confidence.toFixed(2)}
                        </span>
                      )}
                    </td>

                    <td className="px-4 py-3">
                      <InvestigationStatusBadge
                        status={investigation.status}
                        needsHuman={
                          investigation.status === 'completed' &&
                          Boolean(investigation.failure_reason)
                        }
                      />
                    </td>

                    <td className="px-4 py-3 font-mono text-[11px] text-slate-500">
                      {investigation.llm_calls} calls
                      {investigation.total_duration_ms
                        ? ` · ${formatDuration(investigation.total_duration_ms)}`
                        : ''}
                    </td>

                    <td className="px-4 py-3 text-slate-500">
                      {formatRelative(investigation.started_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {investigations.data.length === 0 && (
              <p className="px-4 py-8 text-center text-sm text-slate-500">
                Nothing investigated yet. Start one from an incident.
              </p>
            )}
          </div>
        )}
      </div>
    </>
  );
}
