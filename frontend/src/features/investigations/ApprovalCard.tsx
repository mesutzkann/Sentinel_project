import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { describeError } from '../../api/client';
import { recommendationsApi } from '../../api/endpoints';
import type { RecommendationDto, RecommendationOutcomeDto } from '../../api/types';

/**
 * One proposed fix, and the decision a person makes about it.
 *
 * This is the human in the loop, and the screen is written so that the human can actually be one.
 * Three things follow from that:
 *
 * - **What will run is shown before it runs.** The tool and its arguments are on the card, not
 *   behind a detail view, because "approve" means approving that call and the approval is bound
 *   to those exact arguments.
 * - **Approving takes a minute, and the button says so.** The backend runs the action, waits for
 *   the service to settle and re-measures the symptom before answering. A spinner with no
 *   explanation would read as a hung page.
 * - **The verdict is reported as what it is.** `executed` and `verified` are different outcomes:
 *   one means something was done, the other means the symptom measurably went away. An action
 *   that ran perfectly and changed nothing must not look like a success.
 */
export function ApprovalCard({
  recommendation,
  onSettled,
}: {
  recommendation: RecommendationDto;
  onSettled?: () => void;
}) {
  const [outcome, setOutcome] = useState<RecommendationOutcomeDto | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');

  const approve = useMutation({
    mutationFn: () => recommendationsApi.approve(recommendation.id),
    onSuccess: (result) => {
      setOutcome(result);
      onSettled?.();
    },
  });

  const reject = useMutation({
    mutationFn: () => recommendationsApi.reject(recommendation.id, reason.trim()),
    onSuccess: (result) => {
      setOutcome(result);
      setRejecting(false);
      onSettled?.();
    },
  });

  const status = outcome?.status ?? recommendation.status;
  const decided = status !== 'pending_approval';
  const busy = approve.isPending || reject.isPending;

  return (
    <li className="rounded border border-ink-800 bg-ink-850 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="identifier text-slate-200">{recommendation.action_code}</span>
        <StatusBadge status={status} />
        {recommendation.tool_name && (
          <span className="font-mono text-[11px] text-slate-500">{recommendation.tool_name}</span>
        )}
      </div>

      <p className="mt-1.5 text-[13px] leading-relaxed text-slate-400">
        {recommendation.description}
      </p>

      {recommendation.tool_args && Object.keys(recommendation.tool_args).length > 0 && (
        <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 rounded bg-ink-950 px-2.5 py-2">
          {Object.entries(recommendation.tool_args).map(([key, value]) => (
            <div key={key} className="text-[11px]">
              <dt className="inline text-slate-500">{key}: </dt>
              <dd className="identifier inline text-slate-300">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}

      {!decided && recommendation.requires_approval && (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-primary py-1.5 text-xs"
              disabled={busy}
              onClick={() => approve.mutate()}
            >
              {approve.isPending ? 'Running and measuring...' : 'Approve and run'}
            </button>

            <button
              type="button"
              className="btn-ghost py-1.5 text-xs"
              disabled={busy}
              onClick={() => setRejecting((open) => !open)}
            >
              Reject
            </button>

            {approve.isPending && (
              <span className="text-[11px] text-slate-500">
                This runs the action, waits for the service to settle and measures the symptom
                again. Up to a minute.
              </span>
            )}
          </div>

          {rejecting && (
            <form
              className="mt-2 flex flex-wrap gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (reason.trim()) reject.mutate();
              }}
            >
              <input
                className="field flex-1 min-w-[14rem] py-1.5 text-xs"
                aria-label="Why not"
                placeholder="Why not — the next investigation will read this"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
              <button
                type="submit"
                className="btn-ghost py-1.5 text-xs"
                disabled={reason.trim().length === 0 || busy}
              >
                {reject.isPending ? 'Recording...' : 'Record the refusal'}
              </button>
            </form>
          )}
        </>
      )}

      {(approve.error || reject.error) && (
        <p className="mt-2 rounded border border-state-bad/30 bg-state-bad/10 px-2.5 py-1.5 text-[12px] text-state-bad">
          {describeError(approve.error ?? reject.error)}
        </p>
      )}

      {outcome && <Outcome outcome={outcome} />}
    </li>
  );
}

function Outcome({ outcome }: { outcome: RecommendationOutcomeDto }) {
  // Confirmed is the only green. An action that ran and changed nothing is not a success, and a
  // screen that painted it green would be the screen that closes an ongoing incident.
  const tone = outcome.confirmed
    ? 'border-state-ok/30 bg-state-ok/10 text-state-ok'
    : outcome.status === 'rejected'
      ? 'border-ink-700 bg-ink-900 text-slate-400'
      : 'border-state-warn/30 bg-state-warn/10 text-state-warn';

  return (
    <div className={`mt-3 rounded border px-3 py-2 ${tone}`}>
      <div className="text-[11px] font-medium uppercase tracking-wide">
        {outcome.verdict}
        {outcome.executed && !outcome.confirmed && ' — ran, and the symptom is still there'}
      </div>
      <p className="mt-1 text-[12px] leading-relaxed text-slate-300">{outcome.summary}</p>
      {outcome.error && <p className="mt-1 text-[12px] text-state-bad">{outcome.error}</p>}
    </div>
  );
}

const statusLabels: Record<string, string> = {
  pending_approval: 'needs approval',
  approved: 'approved',
  rejected: 'rejected',
  executing: 'running',
  executed: 'executed',
  verified: 'verified',
  failed: 'failed',
};

const statusStyles: Record<string, string> = {
  pending_approval: 'border-state-warn/30 bg-state-warn/10 text-state-warn',
  approved: 'border-accent/30 bg-accent/10 text-accent',
  executing: 'border-accent/30 bg-accent/10 text-accent',
  verified: 'border-state-ok/30 bg-state-ok/10 text-state-ok',
  executed: 'border-state-warn/30 bg-state-warn/10 text-state-warn',
  rejected: 'border-ink-700 bg-ink-900 text-slate-500',
  failed: 'border-state-bad/30 bg-state-bad/10 text-state-bad',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${
        statusStyles[status] ?? 'border-ink-700 bg-ink-900 text-slate-500'
      }`}
    >
      {statusLabels[status] ?? status}
    </span>
  );
}
