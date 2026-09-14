import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { describeError } from '../../api/client';
import {
  modelsApi,
  modelsQueryKeys,
  type BenchmarkRow,
  type ModelSummary,
  type RouteAnswer,
  type RouterSummary,
} from '../../api/models';

/**
 * What the system runs, and what the router it runs is worth.
 *
 * The page publishes the benchmark's own report rather than numbers written into the UI. That is
 * the whole point: Phase 8's claim is that a fine-tuned 1.5B beats a keyword table on the
 * questions this team actually asks, and a claim rendered from a hard-coded table would survive
 * the model getting worse. When nobody has run the benchmark on this checkout the page says so
 * and gives the command, because no number is better than a stale one.
 *
 * The question box is the same claim in the other direction: one question, both routers, side by
 * side. It is also the fastest way to see the fallback at work — when the tuned model is not
 * imported, both columns come back identical, because the model router answered with the table.
 */
export function ModelsPage() {
  const { data, isPending, error } = useQuery({
    queryKey: modelsQueryKeys.list,
    queryFn: () => modelsApi.list(),
  });

  return (
    <>
      <PageHeader
        title="Models"
        description="The local models this system runs, and the measured accuracy of the router that decides what an investigation collects."
      />

      <div className="space-y-6 p-8">
        {isPending && <p className="text-sm text-slate-500">Asking the AI service...</p>}

        {error && (
          <div className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            <p>{describeError(error)}</p>
            <p className="mt-1 text-slate-400">
              The AI service is not answering. Start it with{' '}
              <span className="identifier">uvicorn app.main:app --port 8000</span>.
            </p>
          </div>
        )}

        {data && (
          <>
            <ModelStrip models={data.models} router={data.router} />
            <Benchmark rows={data.benchmark?.rows ?? []} meta={data.benchmark} />
            <RouteTrier />
          </>
        )}
      </div>
    </>
  );
}

const purposeLabels: Record<string, string> = {
  reasoning: 'Reasoning',
  routing: 'Routing',
  embedding: 'Embedding',
};

function ModelStrip({ models, router }: { models: ModelSummary[]; router: RouterSummary }) {
  return (
    <div className="panel p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-100">In use</h2>

        <p className="text-xs text-slate-400">
          Routing with{' '}
          <span className="identifier text-slate-200">{router.active}</span>
          {router.fallback && (
            <>
              , falling back to <span className="identifier text-slate-200">{router.fallback}</span>{' '}
              when the model is unparseable or absent
            </>
          )}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        {models.map((model) => (
          <div
            key={model.purpose}
            className={`rounded border px-3 py-2.5 ${
              model.available
                ? 'border-ink-700 bg-ink-850'
                : 'border-state-bad/30 bg-state-bad/5'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                {purposeLabels[model.purpose] ?? model.purpose}
              </span>
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  model.available ? 'bg-state-ok' : 'bg-state-bad'
                }`}
                aria-label={model.available ? 'available' : 'unavailable'}
              />
            </div>

            <div className="identifier mt-1 text-slate-100">{model.name}</div>
            <p className="mt-1 text-xs text-slate-500">{model.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function Benchmark({
  rows,
  meta,
}: {
  rows: BenchmarkRow[];
  meta: { generated_at: string | null; split: string | null; prompt: string | null } | null;
}) {
  if (rows.length === 0) {
    return (
      <div className="panel p-4">
        <h2 className="mb-2 text-sm font-semibold text-slate-100">Router benchmark</h2>
        <p className="text-sm text-slate-400">
          Nothing has been measured on this checkout. Run it with{' '}
          <span className="identifier text-slate-300">
            python -m evaluation.router_eval --split test --models sentinel-router --prompt v2
            --output datasets/routing/benchmark.json
          </span>
          .
        </p>
      </div>
    );
  }

  // The strongest row, so the table can mark what the phase is judged on without deciding which
  // router that is in advance -- the benchmark file may hold two rows or five.
  const best = Math.max(...rows.map((row) => row.intent_accuracy));
  const languages = [...new Set(rows.flatMap((row) => Object.keys(row.per_language)))].sort();

  return (
    <div className="panel p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-100">Router benchmark</h2>
        {meta && (
          <p className="text-xs text-slate-500">
            {meta.split} split, prompt {meta.prompt}
            {meta.generated_at && <>, measured {meta.generated_at.slice(0, 10)}</>}
          </p>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-ink-700 text-left text-[11px] uppercase tracking-wide text-slate-500">
              <th className="pb-2 pr-4 font-medium">Router</th>
              <th className="pb-2 pr-4 font-medium">Intent</th>
              <th className="pb-2 pr-4 font-medium">Service</th>
              <th className="pb-2 pr-4 font-medium">Tool F1</th>
              <th className="pb-2 pr-4 font-medium">Invalid JSON</th>
              <th className="pb-2 pr-4 font-medium">p50</th>
              <th className="pb-2 font-medium">p95</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.router} className="border-b border-ink-800 last:border-0">
                <td className="identifier py-2 pr-4 text-slate-200">{row.router}</td>
                <td
                  className={`py-2 pr-4 tabular-nums ${
                    row.intent_accuracy === best
                      ? 'font-semibold text-state-ok'
                      : 'text-slate-300'
                  }`}
                >
                  {percent(row.intent_accuracy)}
                </td>
                <td className="py-2 pr-4 tabular-nums text-slate-300">
                  {percent(row.service_accuracy)}
                </td>
                <td className="py-2 pr-4 tabular-nums text-slate-300">{percent(row.tool_f1)}</td>
                <td className="py-2 pr-4 tabular-nums text-slate-300">
                  {percent(row.invalid_json)}
                </td>
                <td className="py-2 pr-4 tabular-nums text-slate-400">{row.latency_p50} ms</td>
                <td className="py-2 tabular-nums text-slate-400">{row.latency_p95} ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {languages.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">
            Intent accuracy by language
          </h3>

          <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
            {languages.map((language) => (
              <div key={language} className="rounded border border-ink-700 bg-ink-850 px-3 py-2">
                <div className="text-[11px] uppercase tracking-wide text-slate-400">
                  {language}
                </div>
                {rows.map((row) => (
                  <div key={row.router} className="mt-1 flex justify-between text-xs">
                    <span className="identifier truncate text-slate-400">{row.router}</span>
                    <span className="tabular-nums text-slate-200">
                      {percent(row.per_language[language])}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>

          <p className="mt-2 text-xs text-slate-500">
            One test phrasing per intent and language, so a language number rests on fifteen
            phrasings. Read it as direction; the overall figure is the measured one.
          </p>
        </div>
      )}
    </div>
  );
}

function RouteTrier() {
  const [query, setQuery] = useState('');
  const [hint, setHint] = useState('');

  const route = useMutation({
    mutationFn: () => modelsApi.route(query.trim(), hint.trim()),
  });

  const disabled = query.trim().length === 0 || route.isPending;

  return (
    <div className="panel p-4">
      <h2 className="mb-1 text-sm font-semibold text-slate-100">Route a question</h2>
      <p className="mb-3 text-xs text-slate-500">
        The same question through the router the agent uses and through the keyword table it
        replaced. Turkish, English and the mix of the two are all fair game.
      </p>

      <form
        className="flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!disabled) route.mutate();
        }}
      >
        <input
          className="field flex-1 min-w-[16rem]"
          placeholder="payments neden bu kadar yavaş"
          aria-label="Question"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <input
          className="field w-40"
          placeholder="service hint"
          aria-label="Service hint"
          value={hint}
          onChange={(event) => setHint(event.target.value)}
        />
        <button type="submit" className="btn-primary" disabled={disabled}>
          {route.isPending ? 'Routing...' : 'Route'}
        </button>
      </form>

      {route.error && (
        <p className="mt-3 rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
          {describeError(route.error)}
        </p>
      )}

      {route.data && (
        <div className="mt-4">
          <p className="mb-2 text-xs text-slate-400">
            {route.data.answers.length < 2
              ? 'Only the keyword table is configured, so there is nothing to compare against.'
              : route.data.agree
                ? 'Both routers chose the same intent.'
                : 'The routers disagree — which is what the benchmark above measures.'}
          </p>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {route.data.answers.map((answer) => (
              <AnswerCard key={answer.router} answer={answer} agree={route.data.agree} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AnswerCard({ answer, agree }: { answer: RouteAnswer; agree: boolean }) {
  return (
    <div
      className={`rounded border px-3 py-2.5 ${
        agree ? 'border-ink-700 bg-ink-850' : 'border-state-warn/30 bg-state-warn/5'
      }`}
    >
      <div className="flex items-baseline justify-between">
        <span className="identifier text-slate-300">{answer.router}</span>
        <span className="text-xs tabular-nums text-slate-500">{answer.latency_ms} ms</span>
      </div>

      <div className="mt-1.5 text-sm font-semibold text-slate-100">{answer.intent}</div>
      <div className="text-xs text-slate-400">
        {answer.target_service ? (
          <>
            on <span className="identifier text-slate-300">{answer.target_service}</span>
          </>
        ) : (
          'no single service'
        )}
      </div>

      <ul className="mt-2 space-y-0.5">
        {answer.tools.length === 0 && (
          <li className="text-xs text-slate-500">no tools; answered from the knowledge base</li>
        )}
        {answer.tools.map((tool) => (
          <li key={tool} className="identifier text-xs text-slate-400">
            {tool}
          </li>
        ))}
      </ul>
    </div>
  );
}

function percent(value: number | undefined): string {
  if (value === undefined) return '--';

  return `${(value * 100).toFixed(1)}%`;
}
