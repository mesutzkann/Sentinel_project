import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelsPage } from './ModelsPage';
import { modelsApi, type ModelsResponse, type RouteComparison } from '../../api/models';

vi.mock('../../api/models', async () => {
  const actual = await vi.importActual<typeof import('../../api/models')>('../../api/models');
  return { ...actual, modelsApi: { list: vi.fn(), route: vi.fn() } };
});

const list = vi.mocked(modelsApi.list);
const route = vi.mocked(modelsApi.route);

/** Shaped exactly like the AI service's, including a model that is configured and absent. */
const response: ModelsResponse = {
  models: [
    { purpose: 'reasoning', name: 'qwen2.5:3b-instruct', available: true, detail: 'Loaded and reachable.' },
    {
      purpose: 'routing',
      name: 'sentinel-router',
      available: false,
      detail: 'Not imported. Run: ollama create sentinel-router -f models/sentinel-router/Modelfile',
    },
    { purpose: 'embedding', name: 'bge-m3', available: true, detail: 'Loaded and reachable.' },
  ],
  router: {
    active: 'fine_tuned',
    model: 'sentinel-router',
    prompt_version: 'v2',
    timeout_seconds: 20,
    fallback: 'rule',
  },
  benchmark: {
    generated_at: '2026-09-14T12:00:00+00:00',
    split: 'test',
    prompt: 'v2',
    examples: 333,
    rows: [
      {
        router: 'rule',
        examples: 333,
        intent_accuracy: 0.354,
        service_accuracy: 0.976,
        tool_f1: 0.41,
        invalid_json: 0,
        latency_p50: 0,
        latency_p95: 0,
        per_language: { en: 0.09, tr: 0.29, mixed: 0.67 },
        per_intent: {},
      },
      {
        router: 'sentinel-router [v2]',
        examples: 333,
        intent_accuracy: 0.871,
        service_accuracy: 0.964,
        tool_f1: 0.726,
        invalid_json: 0,
        latency_p50: 1241,
        latency_p95: 2295,
        per_language: { en: 0.98, tr: 0.77, mixed: 0.87 },
        per_intent: {},
      },
    ],
  },
};

const disagreement: RouteComparison = {
  query: 'users için bir trace açar mısın',
  answers: [
    {
      router: 'fine_tuned',
      intent: 'TRACE_QUERY',
      target_service: 'users',
      requires_rag: false,
      requires_mcp: true,
      tools: ['traces-mcp/get_recent_traces'],
      latency_ms: 980,
    },
    {
      router: 'rule',
      intent: 'GENERAL_QUESTION',
      target_service: 'users',
      requires_rag: true,
      requires_mcp: true,
      tools: ['logs-mcp/get_recent_errors', 'metrics-mcp/get_service_metrics'],
      latency_ms: 0,
    },
  ],
  agree: false,
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ModelsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ModelsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    list.mockResolvedValue(response);
  });

  it('reports a configured model that is not there, with the command that fixes it', async () => {
    renderPage();

    // The state somebody opens this page to diagnose: routing got worse, and this is why.
    expect(await screen.findByText('sentinel-router')).toBeInTheDocument();
    expect(
      screen.getByText(/ollama create sentinel-router -f models\/sentinel-router\/Modelfile/),
    ).toBeInTheDocument();
  });

  it('publishes the measured numbers rather than a table written into the page', async () => {
    renderPage();

    expect(await screen.findByText('87.1%')).toBeInTheDocument();
    expect(screen.getByText('35.4%')).toBeInTheDocument();
    expect(screen.getByText(/test split, prompt v2/)).toBeInTheDocument();
    expect(screen.getByText(/measured 2026-09-14/)).toBeInTheDocument();
  });

  it('says nothing has been measured rather than showing a stale table', async () => {
    list.mockResolvedValue({ ...response, benchmark: null });
    renderPage();

    expect(await screen.findByText(/Nothing has been measured on this checkout/)).toBeInTheDocument();
    expect(screen.queryByText('87.1%')).not.toBeInTheDocument();
  });

  it('routes a question through both routers and marks a disagreement', async () => {
    route.mockResolvedValue(disagreement);
    renderPage();

    await screen.findByText('87.1%');
    await userEvent.type(await screen.findByLabelText('Question'), 'users için bir trace açar mısın');
    await userEvent.click(screen.getByRole('button', { name: 'Route' }));

    await waitFor(() => expect(screen.getByText(/The routers disagree/)).toBeInTheDocument());
    expect(screen.getByText('TRACE_QUERY')).toBeInTheDocument();
    expect(screen.getByText('GENERAL_QUESTION')).toBeInTheDocument();
    expect(screen.getByText('traces-mcp/get_recent_traces')).toBeInTheDocument();
    expect(route).toHaveBeenCalledWith('users için bir trace açar mısın', '');
  });

  it('will not route an empty question', async () => {
    renderPage();

    await screen.findByText('87.1%');

    expect(screen.getByRole('button', { name: 'Route' })).toBeDisabled();
    expect(route).not.toHaveBeenCalled();
  });
});
