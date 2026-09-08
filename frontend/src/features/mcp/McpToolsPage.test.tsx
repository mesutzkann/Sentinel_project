import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { McpToolsPage } from './McpToolsPage';
import { mcpApi, type McpToolsResponse } from '../../api/mcp';

vi.mock('../../api/mcp', async () => {
  const actual = await vi.importActual<typeof import('../../api/mcp')>('../../api/mcp');
  return { ...actual, mcpApi: { tools: vi.fn(), call: vi.fn() } };
});

const tools = vi.mocked(mcpApi.tools);
const call = vi.mocked(mcpApi.call);

/**
 * A response shaped exactly like the AI service's, including the two things the page has to
 * handle and a naive fixture would omit: a server that is down, and a tool marked destructive.
 */
const response: McpToolsResponse = {
  total_tools: 3,
  servers: [
    { name: 'logs-mcp', url: 'http://logs/mcp', reachable: true, tool_count: 2, error: null },
    { name: 'metrics-mcp', url: 'http://metrics/mcp', reachable: true, tool_count: 1, error: null },
    {
      name: 'git-mcp',
      url: 'http://git/mcp',
      reachable: false,
      tool_count: 0,
      error: 'ConnectError: All connection attempts failed',
    },
  ],
  tools: [
    {
      name: 'get_service_logs',
      server: 'logs-mcp',
      qualified_name: 'logs-mcp/get_service_logs',
      description: 'Recent log lines for one service, newest first.',
      read_only: true,
      destructive: false,
      input_schema: {
        type: 'object',
        properties: {
          service: { type: 'string', description: 'Logical service name.' },
          minutes: { type: 'integer', default: 15 },
          level: { type: ['string', 'null'], description: 'Optional severity filter.' },
        },
        required: ['service'],
      },
    },
    {
      name: 'get_error_rate',
      server: 'logs-mcp',
      qualified_name: 'logs-mcp/get_error_rate',
      description: 'Share of a service log lines that are errors.',
      read_only: true,
      destructive: false,
      input_schema: { type: 'object', properties: {}, required: [] },
    },
    {
      name: 'restart_container',
      server: 'metrics-mcp',
      qualified_name: 'metrics-mcp/restart_container',
      description: 'Restarts a container. Arrives in Phase 10.',
      read_only: false,
      destructive: true,
      input_schema: {
        type: 'object',
        properties: { name: { type: 'string' } },
        required: ['name'],
      },
    },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <McpToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  tools.mockResolvedValue(response);
});

describe('McpToolsPage', () => {
  it('lists every tool grouped under its server', async () => {
    renderPage();

    expect(await screen.findByText('get_service_logs')).toBeInTheDocument();
    expect(screen.getByText('get_error_rate')).toBeInTheDocument();
    expect(screen.getByText('restart_container')).toBeInTheDocument();
  });

  it('reports how many tools were discovered across how many servers', async () => {
    renderPage();

    // Two of three reachable, which is the number a reader needs when a tool is missing.
    expect(await screen.findByText('3')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('shows why an unreachable server is unreachable', async () => {
    // The whole point of surfacing the reason: a missing tool should be explainable rather than
    // looking like the agent quietly lost a capability.
    renderPage();

    expect(await screen.findByText(/ConnectError: All connection attempts failed/)).toBeInTheDocument();
    expect(screen.getByText('unreachable')).toBeInTheDocument();
  });

  it('marks read-only and destructive tools differently', async () => {
    // This is the classification the policy layer enforces, so it has to be visible rather than
    // implied.
    renderPage();

    expect(await screen.findByText('destructive')).toBeInTheDocument();
    expect(screen.getAllByText('read-only')).toHaveLength(2);
  });

  it('seeds the argument editor from the selected tool schema', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText('get_service_logs'));

    const editor = screen.getByLabelText('Arguments') as HTMLTextAreaElement;
    const seeded = JSON.parse(editor.value);

    expect(seeded).toEqual({ service: 'orders', minutes: 15 });
    // `level` is optional and has no default, so it is left out rather than sent as null.
    expect(seeded).not.toHaveProperty('level');
  });

  it('reseeds when a different tool is selected', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText('get_service_logs'));
    await user.click(screen.getByText('restart_container'));

    const editor = screen.getByLabelText('Arguments') as HTMLTextAreaElement;

    expect(JSON.parse(editor.value)).toEqual({ name: '' });
  });

  it('runs the selected tool and renders what came back', async () => {
    const user = userEvent.setup();
    call.mockResolvedValue({
      tool: 'get_service_logs',
      server: 'logs-mcp',
      success: true,
      latency_ms: 412,
      content: { found: 2, window: 'last 15m' },
      error: null,
    });

    renderPage();

    await user.click(await screen.findByText('get_service_logs'));
    await user.click(screen.getByRole('button', { name: 'Run tool' }));

    await waitFor(() =>
      expect(call).toHaveBeenCalledWith('logs-mcp/get_service_logs', {
        service: 'orders',
        minutes: 15,
      }),
    );

    expect(await screen.findByText('ok')).toBeInTheDocument();
    expect(screen.getByText('412 ms')).toBeInTheDocument();
    expect(screen.getByText(/"found": 2/)).toBeInTheDocument();
  });

  it('renders a refused call as a result rather than swallowing it', async () => {
    // A policy refusal comes back as a 200 with success false. From Phase 7 the agent has to
    // reason about it, and so does whoever is reading this page.
    const user = userEvent.setup();
    call.mockResolvedValue({
      tool: 'restart_container',
      server: 'metrics-mcp',
      success: false,
      latency_ms: 0,
      content: null,
      error: "'metrics-mcp/restart_container' changes state and needs an approval token.",
    });

    renderPage();

    await user.click(await screen.findByText('restart_container'));
    await user.click(screen.getByRole('button', { name: 'Run tool' }));

    expect(await screen.findByText('failed')).toBeInTheDocument();
    expect(screen.getByText(/needs an approval token/)).toBeInTheDocument();
  });

  it('refuses to send arguments that are not valid JSON', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText('get_service_logs'));

    const editor = screen.getByLabelText('Arguments');
    await user.clear(editor);
    await user.type(editor, '{{not json');
    await user.click(screen.getByRole('button', { name: 'Run tool' }));

    expect(call).not.toHaveBeenCalled();
  });

  it('says a tool takes no arguments when its schema is empty', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText('get_error_rate'));

    expect(screen.getByText('Takes no arguments.')).toBeInTheDocument();
  });

  it('rediscovers on demand', async () => {
    // A server that was down at startup stays absent until something asks again.
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'Rediscover' }));

    await waitFor(() => expect(tools).toHaveBeenCalledWith(true));
  });

  it('explains what to start when the AI service is not answering', async () => {
    tools.mockRejectedValue(new Error('Network Error'));

    renderPage();

    const message = await screen.findByText(/AI service is not answering/);
    expect(within(message).getByText('mcp')).toBeInTheDocument();
  });
});
