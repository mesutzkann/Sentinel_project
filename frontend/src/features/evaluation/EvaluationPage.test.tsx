import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EvaluationPage } from './EvaluationPage';
import { evaluationsApi, type EvaluationsResponse } from '../../api/evaluations';

vi.mock('../../api/evaluations', async () => {
  const actual =
    await vi.importActual<typeof import('../../api/evaluations')>('../../api/evaluations');
  return { ...actual, evaluationsApi: { list: vi.fn(), detail: vi.fn() } };
});

const list = vi.mocked(evaluationsApi.list);
const detail = vi.mocked(evaluationsApi.detail);

/** A run where one benchmark measured something and one could not run at all. */
const response: EvaluationsResponse = {
  total: 1,
  command: 'python -m evaluation.suite',
  runs: [
    {
      run_id: '20260914T145257Z',
      started_at: '2026-09-14T14:52:57+00:00',
      duration_ms: 512_000,
      machine: 'Windows AMD64',
      runs: [
        {
          kind: 'router',
          status: 'completed',
          started_at: '2026-09-14T14:52:57+00:00',
          duration_ms: 410_000,
          cases: 333,
          metrics: {
            best_router: 'sentinel-router [v2]',
            intent_accuracy: 0.871,
            service_accuracy: 0.97,
            baseline_router: 'rule',
            baseline_intent_accuracy: 0.354,
            latency_p50: 2051,
            split: 'test',
          },
          detail_file: 'router.json',
          error: null,
        },
        {
          kind: 'rag',
          status: 'failed',
          started_at: '2026-09-14T14:59:00+00:00',
          duration_ms: 900,
          cases: 0,
          metrics: {},
          detail_file: null,
          error: 'EmbeddingUnavailableError: bge-m3 is not loaded',
        },
      ],
    },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <EvaluationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('EvaluationPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    list.mockResolvedValue(response);
  });

  it('draws the tuned router against the baseline it has to beat', async () => {
    /** A model that beats nothing is not a result, so both bars are on the card. */
    renderPage();

    expect(await screen.findByText('87.1%')).toBeInTheDocument();
    expect(screen.getByText('35.4%')).toBeInTheDocument();
    expect(screen.getByText(/intent accuracy on the test split/)).toBeInTheDocument();
  });

  it('shows a benchmark that could not run, with the reason', async () => {
    /** A gap that looks like a zero is worse than no row at all. */
    renderPage();

    expect(await screen.findByText('did not run')).toBeInTheDocument();
    expect(screen.getByText(/bge-m3 is not loaded/)).toBeInTheDocument();
  });

  it('says how much of the suite actually ran', async () => {
    renderPage();

    expect(await screen.findByText('1')).toBeInTheDocument();
    expect(screen.getByText(/of 2 benchmarks ran/)).toBeInTheDocument();
  });

  it('fetches the full run only when somebody asks for it', async () => {
    detail.mockResolvedValue({ scores: [{ router: 'rule', intent_accuracy: 0.354 }] });
    renderPage();

    await screen.findByText('87.1%');
    expect(detail).not.toHaveBeenCalled();

    await userEvent.click(screen.getByText('Every case, as the benchmark wrote it'));

    expect(detail).toHaveBeenCalledWith('20260914T145257Z', 'router');
    expect(await screen.findByText(/intent_accuracy/)).toBeInTheDocument();
  });

  it('gives the command when nothing has been measured', async () => {
    /** "No data" and nothing else makes somebody read the source to find out how data arrives. */
    list.mockResolvedValue({ total: 0, runs: [], command: 'python -m evaluation.suite' });
    renderPage();

    expect(await screen.findByText('Nothing has been measured yet')).toBeInTheDocument();
    expect(screen.getByText('python -m evaluation.suite')).toBeInTheDocument();
  });

  it('does not offer to start a run', async () => {
    /** A benchmark is minutes of GPU; two people opening this page must not start two. */
    renderPage();

    await screen.findByText('87.1%');

    expect(screen.queryByRole('button', { name: /run/i })).not.toBeInTheDocument();
  });
});
