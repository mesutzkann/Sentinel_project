import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { InvestigationStatusBadge } from '../../components/Badges';
import { describeError } from '../../api/client';
import { investigationsApi, queryKeys } from '../../api/endpoints';
import { formatAbsolute, formatDuration } from '../../lib/time';
import { EvidencePanel } from './EvidencePanel';
import { Hypotheses } from './Hypotheses';
import { InvestigationGraph } from './InvestigationGraph';
import { RootCause } from './RootCause';
import { findConfidenceBreakdown } from './confidence';
import { Timeline } from './Timeline';
import { useInvestigationStream } from './useInvestigationStream';

/**
 * One investigation, from the question to the conclusion.
 *
 * Read in one request rather than six: a piece of evidence is an assertion until you can see
 * which step found it and which conclusion cited it, so the parts arrive together and are laid
 * out to be read against each other.
 *
 * **Live, with a fallback that is not silent.** While the run is going the hub pushes each step
 * as it happens. When the hub is not connected the page says so and polls instead — an
 * investigation that is running and a page that has stopped listening look identical otherwise,
 * and the second one is the kind of bug people debug for an hour.
 */
export function InvestigationPage() {
  const { id = '' } = useParams();

  const detail = useQuery({
    queryKey: queryKeys.investigations.detail(id),
    queryFn: () => investigationsApi.get(id),
    enabled: id !== '',
  });

  const investigation = detail.data?.investigation;
  const running = investigation?.status === 'running' || investigation?.status === 'queued';
  const stream = useInvestigationStream(id, running === true);

  // Only when the push channel is not carrying the run. A live hub makes this pure waste.
  useQuery({
    queryKey: [...queryKeys.investigations.detail(id), 'poll'],
    queryFn: () => investigationsApi.get(id),
    enabled: running === true && stream !== 'live',
    refetchInterval: 5000,
    // The result is thrown away; the point is the refetch it triggers on the real query.
    select: () => null,
  });

  if (detail.isPending) {
    return <p className="p-8 text-sm text-slate-500">Loading investigation…</p>;
  }

  if (detail.error || !detail.data || !investigation) {
    return (
      <p className="m-8 rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
        {detail.error ? describeError(detail.error) : 'That investigation does not exist.'}
      </p>
    );
  }

  const { steps, evidence, hypotheses, recommendations, tool_calls: toolCalls } = detail.data;
  const rootCause = detail.data.root_cause;
  const breakdown = findConfidenceBreakdown(steps);

  // A completed run that would not stand behind its conclusion still carries the sentence that
  // says why, which is the most useful thing on the screen for the person now holding it.
  const needsHuman = investigation.status === 'completed' && Boolean(investigation.failure_reason);

  return (
    <>
      <PageHeader
        title={`Investigation of ${investigation.incident_code}`}
        description={investigation.query}
        actions={
          <div className="flex items-center gap-3">
            <StreamIndicator status={stream} running={running === true} />
            <InvestigationStatusBadge status={investigation.status} needsHuman={needsHuman} />
            <Link to="/investigations" className="btn-ghost">
              All investigations
            </Link>
          </div>
        }
      />

      <div className="space-y-6 p-8">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          <Stat label="Steps" value={String(steps.length)} />
          <Stat label="Evidence" value={String(evidence.length)} />
          <Stat label="Tool calls" value={String(investigation.tool_calls || toolCalls.length)} />
          <Stat label="Model calls" value={String(investigation.llm_calls)} />
          <Stat
            label="Tokens"
            value={formatTokens(investigation.prompt_tokens + investigation.completion_tokens)}
          />
          <Stat
            label="Duration"
            value={
              investigation.total_duration_ms
                ? formatDuration(investigation.total_duration_ms)
                : running
                  ? 'running'
                  : '—'
            }
          />
        </div>

        {investigation.failure_reason && !rootCause && (
          <p className="rounded border border-state-warn/30 bg-state-warn/10 px-4 py-3 text-sm leading-relaxed text-state-warn">
            {investigation.failure_reason}
          </p>
        )}

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          <div className="space-y-6">
            {rootCause ? (
              <RootCause
                rootCause={rootCause}
                breakdown={breakdown}
                recommendations={recommendations}
                failureReason={investigation.failure_reason}
                onDecision={() => void detail.refetch()}
              />
            ) : (
              <section className="panel px-5 py-8 text-center text-sm text-slate-500">
                {running
                  ? 'No conclusion yet — the agent is still collecting and reasoning.'
                  : 'This investigation ended without a conclusion.'}
              </section>
            )}

            <section className="panel">
              <div className="flex items-baseline justify-between border-b border-ink-800 px-5 py-3">
                <h2 className="text-sm font-medium text-slate-200">How it got there</h2>
                <span className="text-[11px] text-slate-500">
                  a fact with no line out of it is one the conclusion does not use
                </span>
              </div>
              <InvestigationGraph steps={steps} evidence={evidence} rootCause={rootCause} />
            </section>

            <section className="panel">
              <h2 className="border-b border-ink-800 px-5 py-3 text-sm font-medium text-slate-200">
                Hypotheses
              </h2>
              <Hypotheses hypotheses={hypotheses} />
            </section>

            <section className="panel">
              <h2 className="border-b border-ink-800 px-5 py-3 text-sm font-medium text-slate-200">
                Timeline
              </h2>
              <Timeline steps={steps} toolCalls={toolCalls} running={running === true} />
            </section>
          </div>

          <div className="space-y-6">
            <section className="panel">
              <div className="flex items-baseline justify-between border-b border-ink-800 px-4 py-3">
                <h2 className="text-sm font-medium text-slate-200">Evidence</h2>
                <span className="text-[11px] text-slate-500">
                  cited by number, in collection order
                </span>
              </div>
              <EvidencePanel
                evidence={evidence}
                highlighted={rootCause?.validator_output?.supporting_evidence ?? []}
                contradicting={rootCause?.validator_output?.contradicting_evidence ?? []}
              />
            </section>

            <section className="panel px-4 py-3 text-[11px] leading-relaxed text-slate-500">
              <p>
                Started {formatAbsolute(investigation.started_at)}
                {investigation.completed_at && <> · ended {formatAbsolute(investigation.completed_at)}</>}
              </p>
              {investigation.router_intent && (
                <p className="mt-1">
                  Routed as <span className="identifier text-slate-400">{investigation.router_intent}</span>
                </p>
              )}
            </section>
          </div>
        </div>
      </div>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="panel px-3 py-2.5">
      <p className="text-[10px] uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-0.5 font-mono text-lg text-slate-200">{value}</p>
    </div>
  );
}

function StreamIndicator({ status, running }: { status: string; running: boolean }) {
  if (!running) {
    return null;
  }

  const live = status === 'live';

  return (
    <span
      className={`flex items-center gap-1.5 text-[11px] ${
        live ? 'text-state-ok' : 'text-state-warn'
      }`}
      title={
        live
          ? 'Steps are arriving over the hub as they happen.'
          : 'The hub is not connected, so this page is polling every five seconds instead.'
      }
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          live ? 'animate-pulse bg-state-ok' : 'bg-state-warn'
        }`}
        aria-hidden
      />
      {live ? 'live' : status === 'reconnecting' ? 'reconnecting' : 'polling'}
    </span>
  );
}

function formatTokens(total: number): string {
  return total >= 1000 ? `${(total / 1000).toFixed(1)}k` : String(total);
}
