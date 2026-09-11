/**
 * The confidence score, with the threshold that decides what happens next drawn on it.
 *
 * The number on its own says "0.67" and a person has to remember what that means. The line at
 * 0.70 is what turns it into the sentence the agent acted on: below it, the run stops and
 * recommends nothing, however sound the explanation reads. It is the single most useful mark on
 * this screen, and it costs one `div`.
 */
export const CONFIDENCE_THRESHOLD = 0.7;

export function ConfidenceMeter({
  value,
  threshold = CONFIDENCE_THRESHOLD,
}: {
  value: number;
  threshold?: number;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const clears = clamped >= threshold;

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span
          className={`font-mono text-2xl font-semibold ${
            clears ? 'text-state-ok' : 'text-state-warn'
          }`}
        >
          {clamped.toFixed(2)}
        </span>
        <span className="text-[11px] uppercase tracking-wide text-slate-500">
          {clears ? 'above the threshold' : `below ${threshold.toFixed(2)}`}
        </span>
      </div>

      <div className="relative mt-2 h-2 overflow-hidden rounded-full bg-ink-800">
        <div
          className={`h-full rounded-full ${clears ? 'bg-state-ok' : 'bg-state-warn'}`}
          style={{ width: `${clamped * 100}%` }}
        />
        <div
          className="absolute inset-y-0 w-px bg-slate-400"
          style={{ left: `${threshold * 100}%` }}
          aria-hidden
        />
      </div>
    </div>
  );
}

/**
 * One term of the score, as a labelled bar.
 *
 * The weight is shown beside the value because they answer different questions — how well this
 * term scored, and how much this term was allowed to matter — and a breakdown that showed only
 * the first would make a 0.95 on a quarter-weight term look decisive.
 */
export function ConfidenceTerm({
  name,
  value,
  weight,
}: {
  name: string;
  value: number | null;
  weight: number;
}) {
  return (
    <div className="flex items-center gap-3 text-xs">
      <span className="w-40 shrink-0 text-slate-400">{name.replace(/_/g, ' ')}</span>

      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
        {value !== null && (
          <div className="h-full rounded-full bg-accent/70" style={{ width: `${value * 100}%` }} />
        )}
      </div>

      <span className="w-10 shrink-0 text-right font-mono text-slate-300">
        {value === null ? '—' : value.toFixed(2)}
      </span>
      <span className="w-12 shrink-0 text-right font-mono text-slate-600">
        ×{weight.toFixed(2)}
      </span>
    </div>
  );
}
