import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { describeError } from '../../api/client';
import {
  evaluationQueryKeys,
  evaluationsApi,
  type BenchmarkRun,
  type SuiteRun,
} from '../../api/evaluations';

/**
 * What this system has been measured at, and when.
 *
 * Every number here was produced by a benchmark that wrote its own report; nothing on this page
 * is computed in the browser. That is the property worth protecting — a dashboard that derives
 * its own figures is one that can disagree with the thing it is reporting on, and it always
 * disagrees in the flattering direction.
 *
 * Three things the page refuses to do:
 *
 * - **Draw an empty chart.** A checkout where nobody has run the suite says so, and gives the
 *   command.
 * - **Hide a benchmark that did not run.** A missing dependency shows as a failed row with its
 *   reason, because a gap that looks like a zero is worse than no row at all.
 * - **Start a run.** A benchmark is minutes of GPU. Two people opening this screen during an
 *   incident should not put two of them on the card the investigation needs.
 */
export function EvaluationPage() {
  const { data, isPending, error } = useQuery({
    queryKey: evaluationQueryKeys.list,
    queryFn: () => evaluationsApi.list(),
  });

  const latest = data?.runs[0];

  return (
    <>
      <PageHeader
        title="Evaluation"
        description="Every benchmark this system is judged on: the router against the keyword table it replaced, the four retrievers against each other, and the reasoning models against the same evidence."
      />

      <div className="space-y-6 p-8">
        {isPending && <p className="text-sm text-slate-500">Reading what has been measured...</p>}

        {error && (
          <div className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            <p>{describeError(error)}</p>
            <p className="mt-1 text-slate-400">
              The AI service is not answering. Start it with{' '}
              <span className="identifier">uvicorn app.main:app --port 8000</span>.
            </p>
          </div>
        )}

        {data && data.total === 0 && (
          <div className="panel p-6">
            <h2 className="text-sm font-semibold text-slate-100">Nothing has been measured yet</h2>
            <p className="mt-2 text-sm text-slate-400">
              Run the suite and this page fills in:
            </p>
            <p className="identifier mt-2 rounded bg-ink-950 px-3 py-2 text-slate-300">
              {data.command}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              It needs Ollama for the router and the reasoning comparison, and PostgreSQL with the
              corpus ingested for retrieval. A benchmark that cannot run is recorded as not having
              run, with the reason.
            </p>
          </div>
        )}

        {latest && (
          <>
            <RunHeader run={latest} />

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              {latest.runs.map((benchmark) => (
                <BenchmarkCard key={benchmark.kind} runId={latest.run_id} benchmark={benchmark} />
              ))}
            </div>

            {data && data.runs.length > 1 && <History runs={data.runs} />}
          </>
        )}
      </div>
    </>
  );
}

function RunHeader({ run }: { run: SuiteRun }) {
  const ran = run.runs.filter((benchmark) => benchmark.status === 'completed').length;

  return (
    <div className="panel flex flex-wrap items-center justify-between gap-3 p-4">
      <div>
        <div className="text-sm text-slate-300">
          <span className="font-semibold text-slate-100">{ran}</span> of {run.runs.length}{' '}
          benchmarks ran
        </div>
        <div className="mt-0.5 text-xs text-slate-500">
          {new Date(run.started_at).toLocaleString()} · {Math.round(run.duration_ms / 1000)}s ·{' '}
          {run.machine}
        </div>
      </div>

      <span className="identifier text-xs text-slate-500">{run.run_id}</span>
    </div>
  );
}

function BenchmarkCard({ runId, benchmark }: { runId: string; benchmark: BenchmarkRun }) {
  const [open, setOpen] = useState(false);

  const detail = useQuery({
    queryKey: evaluationQueryKeys.detail(runId, benchmark.kind),
    queryFn: () => evaluationsApi.detail(runId, benchmark.kind),
    enabled: open && benchmark.status === 'completed',
  });

  return (
    <section className="panel p-4">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold text-slate-100">{titles[benchmark.kind] ?? benchmark.kind}</h2>
        <span className="text-xs text-slate-500">
          {benchmark.cases > 0 && `${benchmark.cases} cases · `}
          {Math.round(benchmark.duration_ms / 1000)}s
        </span>
      </div>

      {benchmark.status !== 'completed' ? (
        <div className="rounded border border-state-warn/30 bg-state-warn/10 px-3 py-2">
          <div className="text-[11px] font-medium uppercase tracking-wide text-state-warn">
            did not run
          </div>
          <p className="mt-1 text-xs text-slate-400">{benchmark.error}</p>
        </div>
      ) : (
        <>
          <Metrics benchmark={benchmark} />

          <button
            type="button"
            className="mt-3 text-xs text-slate-500 underline-offset-2 hover:text-accent hover:underline"
            onClick={() => setOpen((shown) => !shown)}
          >
            {open ? 'Hide the full run' : 'Every case, as the benchmark wrote it'}
          </button>

          {open && (
            <pre className="mt-2 max-h-80 overflow-auto rounded bg-ink-950 p-3 text-[11px] leading-relaxed text-slate-400">
              {detail.isPending
                ? 'Loading...'
                : detail.error
                  ? describeError(detail.error)
                  : JSON.stringify(detail.data, null, 2)}
            </pre>
          )}
        </>
      )}
    </section>
  );
}

/** One card's numbers, shaped per benchmark because the comparisons are not alike. */
function Metrics({ benchmark }: { benchmark: BenchmarkRun }) {
  const metrics = benchmark.metrics;

  if (benchmark.kind === 'router') {
    return (
      <Bars
        rows={[
          {
            label: String(metrics.best_router ?? 'tuned'),
            value: asNumber(metrics.intent_accuracy),
            emphasis: true,
          },
          {
            label: String(metrics.baseline_router ?? 'keyword table'),
            value: asNumber(metrics.baseline_intent_accuracy),
          },
        ]}
        caption={`intent accuracy on the ${metrics.split ?? 'test'} split · service ${percent(
          asNumber(metrics.service_accuracy),
        )} · p50 ${metrics.latency_p50 ?? '—'} ms`}
      />
    );
  }

  if (benchmark.kind === 'rag') {
    return (
      <Bars
        rows={[
          { label: 'recall@1', value: asNumber(metrics.recall_at_1) },
          { label: 'recall@3', value: asNumber(metrics.recall_at_3) },
          { label: 'recall@5', value: asNumber(metrics.recall_at_5), emphasis: true },
        ]}
        caption={`${metrics.best_retriever ?? 'best retriever'} · p50 ${
          metrics.latency_p50 ?? '—'
        } ms`}
      />
    );
  }

  if (benchmark.kind === 'reasoning') {
    const models = (metrics.models ?? {}) as Record<string, Record<string, unknown>>;

    return (
      <Bars
        rows={Object.entries(models).map(([name, score]) => ({
          label: name,
          value: asNumber(score.root_cause_accuracy),
        }))}
        caption="root-cause accuracy over the recorded evidence, model against model"
      />
    );
  }

  return (
    <dl className="grid grid-cols-2 gap-2">
      {Object.entries(metrics).map(([key, value]) => (
        <div key={key} className="rounded border border-ink-700 bg-ink-850 px-2.5 py-1.5">
          <dt className="text-[10px] uppercase tracking-wide text-slate-500">{key}</dt>
          <dd className="identifier text-xs text-slate-200">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * A bar per row, drawn in CSS.
 *
 * No chart library: these are three bars on a fixed 0–100 scale, and a dependency that draws
 * them would be three hundred kilobytes to avoid writing a div.
 */
function Bars({
  rows,
  caption,
}: {
  rows: { label: string; value: number | null; emphasis?: boolean }[];
  caption: string;
}) {
  return (
    <div>
      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.label}>
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className={row.emphasis ? 'text-slate-200' : 'text-slate-400'}>
                {row.label}
              </span>
              <span
                className={`tabular-nums ${
                  row.emphasis ? 'font-semibold text-state-ok' : 'text-slate-400'
                }`}
              >
                {percent(row.value)}
              </span>
            </div>

            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-ink-800">
              <div
                className={`h-full rounded-full ${row.emphasis ? 'bg-state-ok' : 'bg-accent/50'}`}
                style={{ width: `${Math.round((row.value ?? 0) * 100)}%` }}
                role="presentation"
              />
            </div>
          </li>
        ))}
      </ul>

      <p className="mt-2 text-[11px] text-slate-500">{caption}</p>
    </div>
  );
}

function History({ runs }: { runs: SuiteRun[] }) {
  return (
    <div className="panel p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-100">Earlier runs</h2>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-ink-700 text-left uppercase tracking-wide text-slate-500">
              <th className="pb-2 pr-4 font-medium">Run</th>
              <th className="pb-2 pr-4 font-medium">Router</th>
              <th className="pb-2 pr-4 font-medium">Recall@5</th>
              <th className="pb-2 font-medium">Ran</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => {
              const router = run.runs.find((benchmark) => benchmark.kind === 'router');
              const rag = run.runs.find((benchmark) => benchmark.kind === 'rag');

              return (
                <tr key={run.run_id} className="border-b border-ink-800 last:border-0">
                  <td className="identifier py-2 pr-4 text-slate-300">{run.run_id}</td>
                  <td className="py-2 pr-4 tabular-nums text-slate-400">
                    {percent(asNumber(router?.metrics.intent_accuracy))}
                  </td>
                  <td className="py-2 pr-4 tabular-nums text-slate-400">
                    {percent(asNumber(rag?.metrics.recall_at_5))}
                  </td>
                  <td className="py-2 text-slate-500">
                    {run.runs.filter((benchmark) => benchmark.status === 'completed').length} of{' '}
                    {run.runs.length}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const titles: Record<string, string> = {
  router: 'Router — tuned against keyword table',
  rag: 'Retrieval — recall over the labelled queries',
  reasoning: 'Reasoning — model against model',
  agent: 'Agent — end to end over the chaos scenarios',
};

function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

function percent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`;
}
