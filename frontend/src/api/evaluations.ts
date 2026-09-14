import { aiApi } from './aiClient';

/**
 * What has been measured on this checkout.
 *
 * Read-only by design: a benchmark run is minutes of GPU, and a page that could start one could
 * start two by accident during an incident. `python -m evaluation.suite` is the command; this is
 * the window onto what it left behind.
 */

export interface BenchmarkRun {
  kind: string;
  status: string;
  started_at: string;
  duration_ms: number;
  cases: number;
  metrics: Record<string, unknown>;
  detail_file: string | null;
  error: string | null;
}

export interface SuiteRun {
  run_id: string;
  started_at: string;
  duration_ms: number;
  machine: string;
  runs: BenchmarkRun[];
}

export interface EvaluationsResponse {
  total: number;
  runs: SuiteRun[];
  /** What to type when there is nothing here. */
  command: string;
}

export const evaluationsApi = {
  list: () => aiApi.get<EvaluationsResponse>('/evaluations').then((r) => r.data),

  detail: (runId: string, kind: string) =>
    aiApi.get<Record<string, unknown>>(`/evaluations/${runId}/${kind}`).then((r) => r.data),
};

export const evaluationQueryKeys = {
  list: ['evaluations'] as const,
  detail: (runId: string, kind: string) => ['evaluations', runId, kind] as const,
};
