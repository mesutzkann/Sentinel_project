import type { EvidenceDto, InvestigationStepDto, RootCauseDto } from '../../api/types';

/**
 * How the conclusion came about: what the agent looked at, what it found, and which of it the
 * conclusion actually rests on.
 *
 * **Laid out by hand, in three columns, as plain SVG.** A force-directed graph would place these
 * nodes somewhere different on every render and somewhere different again on the next
 * investigation — and the one question this picture answers is *which facts the conclusion rests
 * on*, which is a question about edges, not about clustering. Columns make that answerable at a
 * glance and make two investigations comparable; a physics simulation makes it a puzzle. It also
 * saves a 200 kB dependency for something that is forty lines of coordinates.
 *
 * The most useful thing on it is the evidence with no outgoing edge: facts the agent collected
 * and the conclusion does not use. Six of the fifteen chaos scenarios produce evidence like that
 * on purpose, and an investigation that cited everything would be one that discriminated nothing.
 */
const COLLECTOR_STATES = new Set([
  'COLLECT_LOGS',
  'COLLECT_METRICS',
  'COLLECT_TRACES',
  'COLLECT_DATABASE',
  'CHECK_DEPLOYMENTS',
  'SEARCH_HISTORY',
  'INSPECT_CODE',
]);

const REASONING_STATES = [
  'GENERATE_HYPOTHESES',
  'RANK_HYPOTHESES',
  'SELECT_ROOT_CAUSE',
  'VALIDATE',
  'RECOMMEND_FIX',
];

const COLUMN = { collector: 130, evidence: 430, reason: 700, conclusion: 700 };
const ROW_HEIGHT = 30;
const TOP = 44;

export function InvestigationGraph({
  steps,
  evidence,
  rootCause,
}: {
  steps: InvestigationStepDto[];
  evidence: EvidenceDto[];
  rootCause: RootCauseDto | null;
}) {
  const collectors = steps.filter((step) => COLLECTOR_STATES.has(step.state));

  if (collectors.length === 0 && evidence.length === 0) {
    return (
      <p className="px-5 py-8 text-center text-sm text-slate-500">
        Nothing collected yet, so there is no shape to draw.
      </p>
    );
  }

  // One row per fact, and the collectors spread across the same span so their edges stay short.
  const rows = Math.max(evidence.length, collectors.length, 1);

  const reasoningRan = REASONING_STATES.filter((state) =>
    steps.some((step) => step.state === state),
  );
  const rounds = steps.filter((step) => step.state === 'GENERATE_HYPOTHESES').length;

  // The reasoning column and the conclusion share an x, so the canvas has to be tall enough for
  // both — on a short investigation the facts alone are not, and the conclusion lands on top of
  // the spine.
  const spineBottom = TOP + reasoningRan.length * 20 + (rounds > 1 ? 20 : 0);
  const height = Math.max(TOP + rows * ROW_HEIGHT + 30, spineBottom + 80);
  const conclusionY = height - 40;

  const evidenceY = (index: number) => TOP + index * ROW_HEIGHT + ROW_HEIGHT / 2;
  const collectorY = (index: number) =>
    TOP + ((index + 0.5) * (rows * ROW_HEIGHT)) / Math.max(collectors.length, 1);

  const stepRow = new Map(collectors.map((step, index) => [step.id, collectorY(index)]));
  const cited = new Set(rootCause?.validator_output?.supporting_evidence ?? []);
  const contradicting = new Set(rootCause?.validator_output?.contradicting_evidence ?? []);

  return (
    <div className="overflow-x-auto px-4 py-3">
      <svg
        viewBox={`0 0 900 ${height}`}
        className="min-w-[720px]"
        role="img"
        aria-label="How the conclusion follows from the evidence"
      >
        <Heading x={COLUMN.collector} label="Looked at" />
        <Heading x={COLUMN.evidence} label="Found" />
        <Heading x={COLUMN.conclusion} label="Concluded" />

        {/* Collector to the facts it produced. */}
        {evidence.map((item, index) =>
          item.step_id && stepRow.has(item.step_id) ? (
            <Edge
              key={`c-${item.id}`}
              from={[COLUMN.collector + 92, stepRow.get(item.step_id)!]}
              to={[COLUMN.evidence - 96, evidenceY(index)]}
              tone="dim"
            />
          ) : null,
        )}

        {/* A fact to the conclusion, only when the conclusion rests on it. The facts with no line
            out of them are the ones the agent collected and did not use — which is the point. */}
        {rootCause &&
          evidence.map((item, index) =>
            cited.has(index) || contradicting.has(index) ? (
              <Edge
                key={`r-${item.id}`}
                from={[COLUMN.evidence + 96, evidenceY(index)]}
                to={[COLUMN.conclusion - 60, conclusionY]}
                tone={contradicting.has(index) ? 'bad' : 'ok'}
              />
            ) : null,
          )}

        {collectors.map((step, index) => (
          <StateNode
            key={step.id}
            x={COLUMN.collector}
            y={collectorY(index)}
            label={step.state.replace('COLLECT_', '').replace('CHECK_', '')}
            detail={`${evidence.filter((e) => e.step_id === step.id).length} fact(s)`}
          />
        ))}

        {evidence.map((item, index) => (
          <EvidenceNode
            key={item.id}
            x={COLUMN.evidence}
            y={evidenceY(index)}
            index={index}
            weight={item.weight}
            summary={item.summary}
            used={cited.has(index)}
            contradicts={contradicting.has(index)}
          />
        ))}

        {/* The reasoning spine, stacked rather than strung out: the order is already the
            timeline's job, and this column is here to say the states ran and where they led. */}
        {reasoningRan.map((state, index) => (
          <g key={state}>
            <text
              x={COLUMN.reason}
              y={TOP + index * 20}
              className="fill-slate-500 font-mono text-[9px]"
            >
              {state}
            </text>
            {index > 0 && (
              <line
                x1={COLUMN.reason - 6}
                y1={TOP + (index - 1) * 20 + 3}
                x2={COLUMN.reason - 6}
                y2={TOP + index * 20 - 7}
                className="stroke-ink-600"
                strokeWidth={1}
              />
            )}
          </g>
        ))}

        {rounds > 1 && (
          <text
            x={COLUMN.reason}
            y={TOP + reasoningRan.length * 20 + 6}
            className="fill-state-warn font-mono text-[9px]"
          >
            ↺ {rounds} rounds — the critic sent one back
          </text>
        )}

        {rootCause && (
          <ConclusionNode
            x={COLUMN.conclusion}
            y={conclusionY}
            category={rootCause.category}
            confidence={rootCause.confidence}
          />
        )}
      </svg>
    </div>
  );
}

function Heading({ x, label }: { x: number; label: string }) {
  return (
    <text x={x} y={20} textAnchor="middle" className="fill-slate-600 text-[10px] uppercase">
      {label}
    </text>
  );
}

/**
 * A curve rather than a straight line: with thirteen facts fanning out of four collectors,
 * straight lines cross at angles that read as a mesh, and a bezier keeps each one followable.
 */
function Edge({
  from,
  to,
  tone,
}: {
  from: [number, number];
  to: [number, number];
  tone: 'dim' | 'ok' | 'bad';
}) {
  const midpoint = (from[0] + to[0]) / 2;
  const stroke =
    tone === 'ok' ? 'stroke-state-ok/50' : tone === 'bad' ? 'stroke-state-bad/50' : 'stroke-ink-700';

  return (
    <path
      d={`M ${from[0]} ${from[1]} C ${midpoint} ${from[1]}, ${midpoint} ${to[1]}, ${to[0]} ${to[1]}`}
      className={stroke}
      fill="none"
      strokeWidth={tone === 'dim' ? 1 : 1.5}
    />
  );
}

function StateNode({
  x,
  y,
  label,
  detail,
}: {
  x: number;
  y: number;
  label: string;
  detail: string;
}) {
  return (
    <g>
      <rect
        x={x - 92}
        y={y - 13}
        width={184}
        height={26}
        rx={4}
        className="fill-ink-850 stroke-accent/30"
      />
      <text x={x - 82} y={y + 1} className="fill-accent font-mono text-[10px]">
        {label}
      </text>
      <text x={x + 82} y={y + 1} textAnchor="end" className="fill-slate-600 font-mono text-[9px]">
        {detail}
      </text>
    </g>
  );
}

function EvidenceNode({
  x,
  y,
  index,
  weight,
  summary,
  used,
  contradicts,
}: {
  x: number;
  y: number;
  index: number;
  weight: number;
  summary: string;
  used: boolean;
  contradicts: boolean;
}) {
  const border = contradicts
    ? 'stroke-state-bad/50'
    : used
      ? 'stroke-state-ok/50'
      : 'stroke-ink-700';

  return (
    <g>
      <title>{summary}</title>
      <rect
        x={x - 96}
        y={y - 11}
        width={192}
        height={22}
        rx={3}
        className={`fill-ink-900 ${border}`}
      />
      <text
        x={x - 88}
        y={y + 3}
        className={`font-mono text-[9px] ${used ? 'fill-state-ok' : 'fill-slate-600'}`}
      >
        [{index}]
      </text>
      <text x={x - 68} y={y + 3} className="fill-slate-400 text-[10px]">
        {truncate(summary, 32)}
      </text>
      {/* Weight as a bar on the right edge, so the strong facts are findable down the column. */}
      <rect x={x + 62} y={y - 3} width={28} height={5} rx={2} className="fill-ink-800" />
      <rect
        x={x + 62}
        y={y - 3}
        width={28 * Math.min(1, weight)}
        height={5}
        rx={2}
        className={used ? 'fill-state-ok/70' : 'fill-ink-600'}
      />
    </g>
  );
}

function ConclusionNode({
  x,
  y,
  category,
  confidence,
}: {
  x: number;
  y: number;
  category: string;
  confidence: number;
}) {
  const clears = confidence >= 0.7;

  return (
    <g>
      <rect
        x={x - 60}
        y={y - 22}
        width={180}
        height={44}
        rx={5}
        className={`fill-ink-850 ${clears ? 'stroke-state-ok/60' : 'stroke-state-warn/60'}`}
        strokeWidth={1.5}
      />
      <text x={x - 50} y={y - 4} className="fill-slate-200 font-mono text-[9px]">
        {truncate(category, 24)}
      </text>
      <text
        x={x - 50}
        y={y + 12}
        className={`font-mono text-[11px] ${clears ? 'fill-state-ok' : 'fill-state-warn'}`}
      >
        {confidence.toFixed(2)}
        <tspan className="fill-slate-600"> {clears ? 'acted on' : 'below the threshold'}</tspan>
      </text>
    </g>
  );
}

function truncate(text: string, length: number): string {
  return text.length <= length ? text : `${text.slice(0, length - 1)}…`;
}
