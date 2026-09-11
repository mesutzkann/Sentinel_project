import type { HypothesisDto } from '../../api/types';

/**
 * What the agent considered, and how far apart it held them.
 *
 * The gap between the first two is a term of the confidence score, so this list is not decoration:
 * a winner that led by 0.02 and one that led by 0.30 are different claims about how settled the
 * conclusion is, and that is visible here and nowhere else.
 *
 * **Rank 0 means superseded.** The critic can send a rejected conclusion back for another round,
 * and each round ranks from one. The rounds that were thrown away keep the score they had and
 * sort last, because losing them entirely would hide that the agent changed its mind.
 */
export function Hypotheses({ hypotheses }: { hypotheses: HypothesisDto[] }) {
  if (hypotheses.length === 0) {
    return <p className="px-5 py-6 text-sm text-slate-500">No hypotheses yet.</p>;
  }

  const ranked = hypotheses.filter((h) => h.rank > 0);
  const superseded = hypotheses.filter((h) => h.rank === 0);
  const leader = ranked[0];
  const runnerUp = ranked[1];

  return (
    <div className="px-5 py-4">
      <ul className="space-y-2">
        {ranked.map((hypothesis) => (
          <Row key={hypothesis.id} hypothesis={hypothesis} />
        ))}
      </ul>

      {leader && runnerUp && (
        <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
          The leader is {(leader.score - runnerUp.score).toFixed(2)} clear of the next explanation
          that rests on different evidence. That gap is one of the terms of the confidence score.
        </p>
      )}

      {superseded.length > 0 && (
        <div className="mt-4">
          <p className="text-[11px] uppercase tracking-wide text-slate-600">
            From a round the critic sent back
          </p>
          <ul className="mt-2 space-y-2 opacity-60">
            {superseded.map((hypothesis) => (
              <Row key={hypothesis.id} hypothesis={hypothesis} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Row({ hypothesis }: { hypothesis: HypothesisDto }) {
  return (
    <li className="flex items-start gap-3">
      <span
        className={`mt-0.5 w-5 shrink-0 text-right font-mono text-[11px] ${
          hypothesis.is_selected ? 'text-state-ok' : 'text-slate-600'
        }`}
      >
        {hypothesis.rank > 0 ? `#${hypothesis.rank}` : '—'}
      </span>

      <span className="mt-1.5 h-1 w-16 shrink-0 overflow-hidden rounded-full bg-ink-800">
        <span
          className={`block h-full rounded-full ${
            hypothesis.is_selected ? 'bg-state-ok' : 'bg-ink-600'
          }`}
          style={{ width: `${Math.max(0, Math.min(1, hypothesis.score)) * 100}%` }}
        />
      </span>

      <span className="w-9 shrink-0 font-mono text-[11px] text-slate-500">
        {hypothesis.score.toFixed(2)}
      </span>

      <span className="min-w-0 flex-1">
        <span
          className={`block text-sm leading-snug ${
            hypothesis.is_selected ? 'text-slate-200' : 'text-slate-400'
          }`}
        >
          {hypothesis.title}
        </span>
        {hypothesis.description && (
          <span className="mt-0.5 block text-[12px] leading-relaxed text-slate-500">
            {hypothesis.description}
          </span>
        )}
      </span>
    </li>
  );
}
