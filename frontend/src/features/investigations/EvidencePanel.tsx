import { SourceBadge } from '../../components/Badges';
import type { EvidenceDto } from '../../api/types';
import { JsonBlock } from './JsonBlock';

/**
 * Every fact the agent collected, in the order it collected them.
 *
 * **The order is the index.** A hypothesis cites evidence by number and so does the critic, and
 * those numbers are positions in this list — so it is never sorted by weight or grouped by
 * source, however much better either would look. The number beside each row is what the
 * citations elsewhere on this page point at.
 *
 * The weight is the collectors' own: 0.9 a saturation the server measured, 0.8 looked and found
 * something, 0.6 a gauge, 0.4 looked and found nothing. The last of those is the interesting one
 * — a negative finding is evidence, and it is what rules an explanation out.
 */
export function EvidencePanel({
  evidence,
  highlighted = [],
  contradicting = [],
}: {
  evidence: EvidenceDto[];
  /** Indices the conclusion rests on, from the critic's reading. */
  highlighted?: number[];
  /** Indices the critic found inconsistent with the conclusion. */
  contradicting?: number[];
}) {
  if (evidence.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-slate-500">
        Nothing collected yet.
      </p>
    );
  }

  const supports = new Set(highlighted);
  const contradicts = new Set(contradicting);

  return (
    <ul className="divide-y divide-ink-800">
      {evidence.map((item, index) => (
        <li
          key={item.id}
          className={`px-4 py-3 ${
            contradicts.has(index)
              ? 'bg-state-bad/5'
              : supports.has(index)
                ? 'bg-state-ok/5'
                : ''
          }`}
        >
          <div className="flex items-start gap-3">
            <span
              className={`mt-0.5 w-6 shrink-0 text-right font-mono text-[11px] ${
                supports.has(index) ? 'text-state-ok' : 'text-slate-600'
              }`}
            >
              [{index}]
            </span>

            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <SourceBadge source={item.source} />
                <WeightBar weight={item.weight} />
                {contradicts.has(index) && (
                  <span className="text-[10px] font-medium uppercase tracking-wide text-state-bad">
                    contradicts
                  </span>
                )}
              </div>

              <p className="mt-1.5 break-words text-sm leading-relaxed text-slate-300">
                {item.summary}
              </p>

              <JsonBlock value={item.raw} label="tool output" />
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * Weight as a bar rather than a number.
 *
 * It is the multiplier on how much this fact counted towards the confidence score, and four
 * values repeated down a list read as a pattern much faster as bars than as "0.80".
 */
function WeightBar({ weight }: { weight: number }) {
  return (
    <span className="flex items-center gap-1.5" title={`weight ${weight.toFixed(2)}`}>
      <span className="h-1 w-12 overflow-hidden rounded-full bg-ink-800">
        <span
          className={`block h-full rounded-full ${
            weight >= 0.8 ? 'bg-accent' : weight >= 0.6 ? 'bg-accent/60' : 'bg-ink-600'
          }`}
          style={{ width: `${weight * 100}%` }}
        />
      </span>
      <span className="font-mono text-[10px] text-slate-600">{weight.toFixed(2)}</span>
    </span>
  );
}
