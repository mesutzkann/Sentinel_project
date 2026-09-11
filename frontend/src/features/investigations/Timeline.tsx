import type { InvestigationStepDto, ToolCallDto } from '../../api/types';
import { formatDuration } from '../../lib/time';
import { JsonBlock } from './JsonBlock';

/**
 * What the agent did, in the order it did it.
 *
 * **The sequence numbers have gaps and that is correct.** They count every callback event, and
 * evidence, hypotheses and the root cause consume numbers between the steps — a healthy run is
 * numbered 1, 2, 3, 6, 9. Showing them anyway is deliberate: the number is what a step is keyed
 * by in the database and in the AI service's own log, and hiding it would make those two harder
 * to line up than the gaps make this list look odd.
 */
const stateKinds: Record<string, 'collect' | 'reason' | 'decide' | 'plan'> = {
  UNDERSTAND_INCIDENT: 'plan',
  PLAN: 'plan',
  COLLECT_LOGS: 'collect',
  COLLECT_METRICS: 'collect',
  COLLECT_TRACES: 'collect',
  COLLECT_DATABASE: 'collect',
  CHECK_DEPLOYMENTS: 'collect',
  SEARCH_HISTORY: 'collect',
  INSPECT_CODE: 'collect',
  COLLECT_ADDITIONAL_EVIDENCE: 'collect',
  GENERATE_HYPOTHESES: 'reason',
  RANK_HYPOTHESES: 'reason',
  SELECT_ROOT_CAUSE: 'decide',
  VALIDATE: 'decide',
  RECOMMEND_FIX: 'decide',
};

const kindStyles = {
  plan: 'border-ink-600 bg-ink-800 text-slate-400',
  collect: 'border-accent/30 bg-accent/10 text-accent',
  reason: 'border-severity-low/30 bg-severity-low/10 text-severity-low',
  decide: 'border-state-ok/30 bg-state-ok/10 text-state-ok',
} as const;

export function Timeline({
  steps,
  toolCalls,
  running,
}: {
  steps: InvestigationStepDto[];
  toolCalls: ToolCallDto[];
  running: boolean;
}) {
  if (steps.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-slate-500">
        {running ? 'The agent has not reported a step yet.' : 'This investigation recorded no steps.'}
      </p>
    );
  }

  const callsByStep = new Map<string, ToolCallDto[]>();

  for (const call of toolCalls) {
    if (call.step_id) {
      callsByStep.set(call.step_id, [...(callsByStep.get(call.step_id) ?? []), call]);
    }
  }

  return (
    <ol className="relative space-y-1 py-2 pl-8 pr-4">
      {/* The rail. Inset so the state chips sit on it rather than beside it. */}
      <div className="absolute bottom-4 left-[14px] top-4 w-px bg-ink-800" aria-hidden />

      {steps.map((step) => (
        <li key={step.id} className="relative">
          <span
            className={`absolute -left-[26px] top-1.5 h-2.5 w-2.5 rounded-full border-2
                        border-ink-900 ${
                          stateKinds[step.state] === 'decide' ? 'bg-state-ok' : 'bg-ink-600'
                        }`}
            aria-hidden
          />

          <div className="py-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-slate-600">#{step.sequence}</span>
              <span
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] tracking-tight ${
                  kindStyles[stateKinds[step.state] ?? 'plan']
                }`}
              >
                {step.state}
              </span>
              {step.duration_ms !== null && step.duration_ms > 0 && (
                <span className="font-mono text-[11px] text-slate-600">
                  {formatDuration(step.duration_ms)}
                </span>
              )}
            </div>

            <p className="mt-1 text-sm leading-relaxed text-slate-300">{step.message}</p>

            {callsByStep.get(step.id)?.map((call) => (
              <div key={call.id} className="mt-1 flex items-center gap-2 text-[11px]">
                <span className={call.success ? 'text-slate-500' : 'text-state-bad'}>
                  {call.success ? '·' : '×'}
                </span>
                <span className="font-mono text-slate-500">
                  {call.server}/{call.tool}
                </span>
                <span className="font-mono text-slate-600">{call.latency_ms}ms</span>
              </div>
            ))}

            <JsonBlock value={step.payload} label="step payload" />
          </div>
        </li>
      ))}

      {running && (
        <li className="relative">
          <span
            className="absolute -left-[26px] top-2 h-2.5 w-2.5 animate-pulse rounded-full
                       border-2 border-ink-900 bg-accent"
            aria-hidden
          />
          <p className="py-1.5 text-sm text-slate-500">Working…</p>
        </li>
      )}
    </ol>
  );
}
