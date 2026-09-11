import type { RecommendationDto, RootCauseDto } from '../../api/types';
import { ConfidenceMeter, ConfidenceTerm } from './ConfidenceMeter';
import type { ConfidenceBreakdown } from './confidence';

/**
 * The conclusion, and everything that decided whether the system would stand behind it.
 *
 * Four things are on this card and only the first is the answer: what the agent concluded, what
 * the score was made of, what the critic objected to, and what it proposes doing. A screen that
 * showed the conclusion alone would be asking to be trusted; this one is showing its work.
 */
export function RootCause({
  rootCause,
  breakdown,
  recommendations,
  failureReason,
}: {
  rootCause: RootCauseDto;
  breakdown: ConfidenceBreakdown | null;
  recommendations: RecommendationDto[];
  failureReason: string | null;
}) {
  const verdict = rootCause.validator_output;

  return (
    <section className="panel">
      <div className="border-b border-ink-800 px-5 py-4">
        <div className="flex items-start justify-between gap-6">
          <div className="min-w-0">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">Root cause</p>
            <h2 className="mt-1 text-lg font-semibold leading-snug text-slate-100">
              {rootCause.title}
            </h2>
            <p className="identifier mt-1.5 text-accent">{rootCause.category}</p>
          </div>

          <div className="w-48 shrink-0">
            <ConfidenceMeter value={rootCause.confidence} threshold={breakdown?.threshold} />
          </div>
        </div>

        {rootCause.explanation && (
          <p className="mt-4 whitespace-pre-line text-sm leading-relaxed text-slate-300">
            {rootCause.explanation}
          </p>
        )}
      </div>

      {breakdown && (
        <div className="space-y-2 border-b border-ink-800 px-5 py-4">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            What the score is made of
          </p>

          {Object.entries(breakdown.terms).map(([name, term]) => (
            <ConfidenceTerm key={name} name={name} value={term.value} weight={term.weight} />
          ))}

          {breakdown.missing_terms.length > 0 && (
            <p className="pt-1 text-[11px] leading-relaxed text-slate-500">
              {breakdown.missing_terms.map((t) => t.replace(/_/g, ' ')).join(' and ')}{' '}
              {breakdown.missing_terms.length === 1 ? 'was' : 'were'} not measured on this run, so
              the remaining terms were renormalised to sum to one. A term nothing could measure is
              dropped rather than scored zero, which would read as a measurement.
            </p>
          )}
        </div>
      )}

      {verdict && (
        <div className="border-b border-ink-800 px-5 py-4">
          <div className="flex items-center gap-2">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">The critic</p>
            <span
              className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                verdict.valid
                  ? 'border-state-ok/30 bg-state-ok/10 text-state-ok'
                  : 'border-state-warn/30 bg-state-warn/10 text-state-warn'
              }`}
            >
              {verdict.valid ? 'accepted' : 'voted against'}
            </span>
            {rootCause.validator_confidence !== null && (
              <span className="font-mono text-[11px] text-slate-500">
                its own confidence {rootCause.validator_confidence.toFixed(2)}
              </span>
            )}
          </div>

          {!verdict.valid &&
            verdict.contradicting_evidence.length === 0 &&
            verdict.supporting_evidence.length > 0 &&
            !verdict.alternative && (
              <p className="mt-2 rounded border border-state-warn/20 bg-state-warn/5 px-3 py-2 text-[12px] leading-relaxed text-slate-400">
                It voted against the conclusion while citing{' '}
                {verdict.supporting_evidence.map((i) => `[${i}]`).join(' ')} as supporting it,
                nothing as contradicting it, and offering no better explanation — so the vote was
                overruled and kept as a concern. It still counts: its confidence is a quarter of
                the score above.
              </p>
            )}

          <div className="mt-3 flex flex-wrap gap-4 text-[11px]">
            <Citations label="supports" indices={verdict.supporting_evidence} tone="ok" />
            <Citations label="contradicts" indices={verdict.contradicting_evidence} tone="bad" />
          </div>

          {verdict.concerns.length > 0 && (
            <ul className="mt-3 space-y-1">
              {verdict.concerns.map((concern) => (
                <li key={concern} className="flex gap-2 text-[12px] leading-relaxed text-slate-400">
                  <span className="text-slate-600">—</span>
                  {concern}
                </li>
              ))}
            </ul>
          )}

          {verdict.unsupported_claims.length > 0 && (
            <div className="mt-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-600">
                Claims nothing backs
              </p>
              <ul className="mt-1 space-y-1">
                {verdict.unsupported_claims.map((claim) => (
                  <li key={claim} className="text-[12px] leading-relaxed text-slate-400">
                    {claim}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {verdict.alternative && (
            <p className="mt-3 text-[12px] leading-relaxed text-slate-400">
              <span className="text-slate-600">A better explanation may be: </span>
              {verdict.alternative}
            </p>
          )}
        </div>
      )}

      <div className="px-5 py-4">
        <p className="text-[11px] uppercase tracking-wide text-slate-500">Recommended actions</p>

        {recommendations.length === 0 ? (
          <p className="mt-2 text-[12px] leading-relaxed text-slate-500">
            None.{' '}
            {failureReason ??
              'The agent proposes a fix only for a conclusion it will stand behind.'}
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {recommendations.map((recommendation) => (
              <li key={recommendation.id} className="rounded border border-ink-800 bg-ink-850 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="identifier text-slate-200">{recommendation.action_code}</span>
                  {recommendation.requires_approval && (
                    <span
                      className="rounded border border-state-warn/30 bg-state-warn/10 px-1.5
                                 py-0.5 text-[10px] font-medium text-state-warn"
                    >
                      needs approval
                    </span>
                  )}
                  {recommendation.tool_name && (
                    <span className="font-mono text-[11px] text-slate-500">
                      {recommendation.tool_name}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 text-[13px] leading-relaxed text-slate-400">
                  {recommendation.description}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function Citations({
  label,
  indices,
  tone,
}: {
  label: string;
  indices: number[];
  tone: 'ok' | 'bad';
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-slate-600">{label}</span>
      {indices.length === 0 ? (
        <span className="text-slate-600">nothing</span>
      ) : (
        indices.map((index) => (
          <span
            key={index}
            className={`rounded px-1 font-mono ${
              tone === 'ok' ? 'bg-state-ok/10 text-state-ok' : 'bg-state-bad/10 text-state-bad'
            }`}
          >
            [{index}]
          </span>
        ))
      )}
    </span>
  );
}
