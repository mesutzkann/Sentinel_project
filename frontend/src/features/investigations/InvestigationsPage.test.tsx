import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { InvestigationsPage } from './InvestigationsPage';
import { investigationsApi } from '../../api/endpoints';
import type { InvestigationDto } from '../../api/types';

vi.mock('../../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../../api/endpoints')>(
    '../../api/endpoints',
  );

  return {
    ...actual,
    investigationsApi: { list: vi.fn(), get: vi.fn(), start: vi.fn() },
  };
});

const listed = vi.mocked(investigationsApi.list);

function summary(overrides: Partial<InvestigationDto>): InvestigationDto {
  return {
    id: '11111111-1111-1111-1111-111111111111',
    incident_id: '22222222-2222-2222-2222-222222222222',
    incident_code: 'INC-00013',
    query: 'orders is timing out, find out why',
    status: 'completed',
    confidence: 0.63,
    root_cause_title: 'Application exhausting database connection pool',
    root_cause_category: 'DB_CONNECTION_POOL_EXHAUSTION',
    router_intent: 'PERFORMANCE_ANALYSIS',
    step_count: 21,
    evidence_count: 14,
    tool_calls: 11,
    llm_calls: 7,
    prompt_tokens: 15500,
    completion_tokens: 1200,
    total_duration_ms: 310000,
    failure_reason: null,
    started_at: '2026-09-16T12:00:00Z',
    completed_at: '2026-09-16T12:05:10Z',
    ...overrides,
  } as InvestigationDto;
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <InvestigationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('InvestigationsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a concluded run with its confidence', async () => {
    listed.mockResolvedValue([summary({})]);

    renderPage();

    expect(await screen.findByText('INC-00013')).toBeInTheDocument();
    expect(screen.getByText('0.63')).toBeInTheDocument();
  });

  /**
   * The regression this file was written for. The API serialises with WhenWritingNull, so a
   * running investigation arrives with no `confidence` key at all rather than with a null one.
   * The cell guarded that with `=== null`, `undefined` went to `.toFixed`, and the exception
   * unmounted the whole tree: the page rendered zero characters — not an error, not a spinner,
   * nothing — precisely when someone had opened it to watch a run.
   */
  it('survives a running investigation whose confidence has not been computed', async () => {
    const running = summary({ status: 'running', completed_at: null });
    delete (running as Partial<InvestigationDto>).confidence;

    listed.mockResolvedValue([running]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('INC-00013')).toBeInTheDocument();
    });

    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
