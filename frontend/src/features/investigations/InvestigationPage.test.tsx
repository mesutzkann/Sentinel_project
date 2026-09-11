import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { InvestigationPage } from './InvestigationPage';
import { investigationsApi } from '../../api/endpoints';
import type { InvestigationDetailDto } from '../../api/types';

vi.mock('../../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../../api/endpoints')>(
    '../../api/endpoints',
  );

  return {
    ...actual,
    investigationsApi: { list: vi.fn(), get: vi.fn(), start: vi.fn() },
  };
});

// The hub needs a WebSocket and jsdom has none. What is being tested is what the page renders
// from a given response; the live channel has its own failure path and is asserted through it.
vi.mock('@microsoft/signalr', () => ({
  HubConnectionBuilder: class {
    withUrl() {
      return this;
    }
    withAutomaticReconnect() {
      return this;
    }
    configureLogging() {
      return this;
    }
    build() {
      return {
        state: 'Disconnected',
        on: () => undefined,
        onreconnecting: () => undefined,
        onreconnected: () => undefined,
        onclose: () => undefined,
        start: () => Promise.reject(new Error('no websocket in jsdom')),
        invoke: () => Promise.resolve(),
        stop: () => Promise.resolve(),
      };
    }
  },
  HubConnectionState: { Disconnected: 'Disconnected' },
  LogLevel: { Warning: 3 },
}));

const get = vi.mocked(investigationsApi.get);

/**
 * The pool-exhaustion run, as the backend returns it.
 *
 * Carries the four things a naive fixture would leave out and the page has to be right about: a
 * gap in the step sequence, a hypothesis from a round the critic sent back, a negative finding
 * among the evidence, and a critic that voted against a conclusion it also said was supported.
 */
function detail(overrides: Partial<InvestigationDetailDto> = {}): InvestigationDetailDto {
  return {
    investigation: {
      id: 'inv-1',
      incident_id: 'inc-1',
      incident_code: 'INC-00142',
      query: 'orders is timing out, find out why',
      router_intent: 'FULL_INVESTIGATION',
      status: 'completed',
      started_at: '2026-09-11T13:12:10Z',
      completed_at: '2026-09-11T13:13:56Z',
      total_duration_ms: 106_119,
      llm_calls: 4,
      tool_calls: 10,
      prompt_tokens: 5099,
      completion_tokens: 1200,
      failure_reason: null,
      root_cause_title: 'The connection pool was reduced to 20',
      root_cause_category: 'DB_CONNECTION_POOL_EXHAUSTION',
      confidence: 0.79,
      step_count: 2,
      evidence_count: 3,
    },
    steps: [
      {
        id: 'step-1',
        sequence: 1,
        state: 'COLLECT_DATABASE',
        message: 'COLLECT_DATABASE: 3 fact(s) from 3 call(s)',
        payload: { evidence_added: 3 },
        duration_ms: 412,
        started_at: '2026-09-11T13:12:11Z',
        completed_at: '2026-09-11T13:12:12Z',
      },
      {
        // Sequence 6, not 2: evidence events consume numbers between the steps.
        id: 'step-2',
        sequence: 6,
        state: 'VALIDATE',
        message: 'the critic accepts the conclusion',
        payload: {
          confidence: {
            value: 0.79,
            threshold: 0.7,
            meets_threshold: true,
            terms: {
              evidence_support: { weight: 0.45, value: 0.94 },
              validator_confidence: { weight: 0.25, value: 0.8 },
              historical_similarity: { weight: 0.15, value: null },
              hypothesis_margin: { weight: 0.15, value: 0.28 },
            },
            missing_terms: ['historical_similarity'],
          },
        },
        duration_ms: 8400,
        started_at: '2026-09-11T13:13:40Z',
        completed_at: '2026-09-11T13:13:48Z',
      },
    ],
    evidence: [
      {
        id: 'e-0',
        step_id: 'step-1',
        source: 'database',
        summary: 'database connections: 20 of 20 used, 0 spare',
        weight: 0.9,
        raw: { used: 20, max_connections: 20, headroom: 0 },
        created_at: '2026-09-11T13:12:12Z',
      },
      {
        id: 'e-1',
        step_id: 'step-1',
        source: 'logs',
        summary: 'recent errors: 347 in the last 30 minutes',
        weight: 0.8,
        raw: null,
        created_at: '2026-09-11T13:12:13Z',
      },
      {
        id: 'e-2',
        step_id: 'step-1',
        source: 'database',
        summary: 'deadlocks since reset: 0; sessions blocked now: 0',
        weight: 0.4,
        raw: null,
        created_at: '2026-09-11T13:12:14Z',
      },
    ],
    hypotheses: [
      {
        id: 'h-1',
        title: 'The connection pool is exhausted',
        description: 'Every connection is in use and new work waits.',
        score: 0.81,
        rank: 1,
        is_selected: true,
      },
      {
        id: 'h-2',
        title: 'A slow query is holding connections',
        description: null,
        score: 0.34,
        rank: 2,
        is_selected: false,
      },
      {
        id: 'h-3',
        title: 'A retry storm',
        description: null,
        score: 0.88,
        rank: 0,
        is_selected: false,
      },
    ],
    root_cause: {
      id: 'rc-1',
      hypothesis_id: 'h-1',
      title: 'The connection pool was reduced to 20',
      category: 'DB_CONNECTION_POOL_EXHAUSTION',
      confidence: 0.79,
      explanation: '200 of 200 connections are in use after MaxPoolSize was lowered.',
      validator_output: {
        supporting_evidence: [0, 1],
        contradicting_evidence: [],
        unsupported_claims: [],
        concerns: ['the connection count is for the whole estate'],
        alternative: null,
        valid: true,
        confidence: 0.8,
      },
      validator_confidence: 0.8,
    },
    recommendations: [
      {
        id: 'r-1',
        root_cause_id: 'rc-1',
        action_code: 'RESTORE_POOL_SIZE',
        description: 'Restore MaxPoolSize to 200 and restart orders.',
        tool_name: 'update_env_and_restart',
        tool_args: { service: 'orders' },
        requires_approval: true,
        status: 'pending_approval',
      },
    ],
    tool_calls: [
      {
        id: 't-1',
        step_id: 'step-1',
        server: 'database-mcp',
        tool: 'get_connection_count',
        result_summary: 'database connections: 20 of 20 used',
        success: true,
        latency_ms: 41,
        called_at: '2026-09-11T13:12:12Z',
      },
    ],
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/investigations/inv-1']}>
        <Routes>
          <Route path="/investigations/:id" element={<InvestigationPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('InvestigationPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue(detail());
  });

  it('shows the conclusion, its category and its confidence', async () => {
    renderPage();

    expect(await screen.findByText('The connection pool was reduced to 20')).toBeInTheDocument();
    expect(screen.getAllByText('DB_CONNECTION_POOL_EXHAUSTION').length).toBeGreaterThan(0);
    expect(screen.getByText('0.79')).toBeInTheDocument();
    expect(screen.getByText('above the threshold')).toBeInTheDocument();
  });

  it('breaks the score into the terms it was computed from', async () => {
    // The number on its own asks to be trusted. The terms are the argument for it.
    renderPage();

    const terms = (await screen.findByText('What the score is made of')).closest('div');
    const scoped = within(terms as HTMLElement);

    expect(scoped.getByText('evidence support')).toBeInTheDocument();
    expect(scoped.getByText('0.94')).toBeInTheDocument();

    // A term nothing could measure gets an empty bar and a dash, not a zero.
    expect(scoped.getByText('historical similarity')).toBeInTheDocument();
    expect(scoped.getByText('—')).toBeInTheDocument();
    expect(scoped.getByText(/renormalised to sum to one/)).toBeInTheDocument();
  });

  it('numbers the evidence the way the citations do', async () => {
    // Hypotheses and the critic cite by position in this list, so the order is the contract.
    renderPage();

    await screen.findByText('The connection pool was reduced to 20');

    const panel = screen.getByRole('heading', { name: 'Evidence' }).closest('section');
    const items = within(panel as HTMLElement).getAllByRole('listitem');

    expect(items[0]).toHaveTextContent('[0]');
    expect(items[0]).toHaveTextContent('database connections: 20 of 20 used');
    expect(items[2]).toHaveTextContent('[2]');
    expect(items[2]).toHaveTextContent('deadlocks since reset: 0');
  });

  it('keeps the tool output one click away from the summary', async () => {
    renderPage();

    const button = await screen.findByRole('button', { name: /tool output/ });
    expect(screen.queryByText(/max_connections/)).not.toBeInTheDocument();

    await userEvent.click(button);

    expect(screen.getByText(/max_connections/)).toBeInTheDocument();
  });

  it('sorts a superseded round below the ranking and keeps its score', async () => {
    // The critic sent a round back; that hypothesis outscored the winner in its own round and
    // must not read as the leader now.
    renderPage();

    const heading = await screen.findByRole('heading', { name: 'Hypotheses' });
    const panel = within(heading.closest('section') as HTMLElement);

    expect(panel.getByText('From a round the critic sent back')).toBeInTheDocument();

    const rows = panel.getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('#1');
    expect(rows[0]).toHaveTextContent('The connection pool is exhausted');

    // Last, unranked, and still carrying what it scored at the time.
    expect(rows[2]).toHaveTextContent('A retry storm');
    expect(rows[2]).toHaveTextContent('0.88');
    expect(rows[2]).not.toHaveTextContent('#');
  });

  it('shows the step sequence with its gaps', async () => {
    // Evidence events consume numbers too, so 1 then 6 is a healthy run rather than a loss.
    renderPage();

    const heading = await screen.findByRole('heading', { name: 'Timeline' });
    const panel = within(heading.closest('section') as HTMLElement);

    const steps = panel.getAllByRole('listitem');
    expect(steps[0]).toHaveTextContent('#1');
    expect(steps[0]).toHaveTextContent('COLLECT_DATABASE');
    expect(steps[1]).toHaveTextContent('#6');
    expect(panel.getByText('database-mcp/get_connection_count')).toBeInTheDocument();
  });

  it('says why a completed run recommends nothing', async () => {
    get.mockResolvedValue(
      detail({
        investigation: {
          ...detail().investigation,
          confidence: 0.62,
          failure_reason: 'the conclusion stands but is not certain enough to act on',
        },
        recommendations: [],
      }),
    );

    renderPage();

    expect(await screen.findByText(/not certain enough to act on/)).toBeInTheDocument();
    expect(screen.getByText('Needs a human')).toBeInTheDocument();
  });

  it('explains an overruled veto rather than showing a bare rejection', async () => {
    const base = detail();

    get.mockResolvedValue({
      ...base,
      root_cause: {
        ...base.root_cause!,
        validator_output: {
          supporting_evidence: [0, 1, 2],
          contradicting_evidence: [],
          unsupported_claims: [],
          concerns: ['the evidence does not prove the timeouts came from the pool'],
          alternative: null,
          valid: false,
          confidence: 0.55,
        },
      },
    });

    renderPage();

    expect(await screen.findByText('voted against')).toBeInTheDocument();
    expect(screen.getByText(/the vote was overruled and kept as a concern/)).toBeInTheDocument();
  });

  it('says it is polling when the hub will not connect', async () => {
    // A running investigation and a page that stopped listening look identical otherwise.
    get.mockResolvedValue(
      detail({
        investigation: { ...detail().investigation, status: 'running', completed_at: null },
      }),
    );

    renderPage();

    await waitFor(() => expect(screen.getByText('polling')).toBeInTheDocument());
  });
});
