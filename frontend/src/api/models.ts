import { aiApi } from './aiClient';

/**
 * The models the AI service runs, and what the router is measured at.
 *
 * Against the AI service rather than the backend, for the same reason the MCP endpoints are:
 * the service owns its own model configuration, and proxying it would mean the backend
 * re-declaring a contract it has no part in.
 */

export type ModelPurpose = 'reasoning' | 'routing' | 'embedding';

export interface ModelSummary {
  purpose: ModelPurpose;
  name: string;
  available: boolean;
  detail: string;
}

export interface RouterSummary {
  active: string;
  model: string | null;
  prompt_version: string | null;
  timeout_seconds: number | null;
  fallback: string | null;
}

export interface BenchmarkRow {
  router: string;
  examples: number;
  intent_accuracy: number;
  service_accuracy: number;
  tool_f1: number;
  invalid_json: number;
  latency_p50: number;
  latency_p95: number;
  per_language: Record<string, number>;
  per_intent: Record<string, number>;
}

export interface Benchmark {
  generated_at: string | null;
  split: string | null;
  prompt: string | null;
  examples: number;
  rows: BenchmarkRow[];
}

export interface ModelsResponse {
  models: ModelSummary[];
  router: RouterSummary;
  benchmark: Benchmark | null;
}

export interface RouteAnswer {
  router: string;
  intent: string;
  target_service: string | null;
  requires_rag: boolean;
  requires_mcp: boolean;
  tools: string[];
  latency_ms: number;
}

export interface RouteComparison {
  query: string;
  answers: RouteAnswer[];
  agree: boolean;
}

export const modelsApi = {
  list: () => aiApi.get<ModelsResponse>('/models').then((r) => r.data),

  route: (query: string, serviceHint?: string) =>
    aiApi
      .post<RouteComparison>('/models/route', {
        query,
        service_hint: serviceHint || null,
      })
      .then((r) => r.data),
};

export const modelsQueryKeys = {
  list: ['models'] as const,
};
