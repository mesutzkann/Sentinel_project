import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApprovalCard } from './ApprovalCard';
import { recommendationsApi } from '../../api/endpoints';
import type { RecommendationDto, RecommendationOutcomeDto } from '../../api/types';

vi.mock('../../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../../api/endpoints')>('../../api/endpoints');
  return { ...actual, recommendationsApi: { approve: vi.fn(), reject: vi.fn() } };
});

const approve = vi.mocked(recommendationsApi.approve);
const reject = vi.mocked(recommendationsApi.reject);

const recommendation: RecommendationDto = {
  id: 'rec-1',
  root_cause_id: 'rc-1',
  action_code: 'RESTORE_POOL_SIZE',
  description: 'Restore MaxPoolSize to 200 and restart orders.',
  tool_name: 'docker-mcp/update_env_and_restart',
  tool_args: { name: 'sentinel-orders', environment: 'MaxPoolSize=200' },
  requires_approval: true,
  status: 'pending_approval',
};

const resolved: RecommendationOutcomeDto = {
  id: 'rec-1',
  status: 'verified',
  executed: true,
  confirmed: true,
  verdict: 'resolved',
  summary: 'The error rate went from 18.0% to 0.2%: the symptom is gone.',
  error: null,
};

const ranAndDidNothing: RecommendationOutcomeDto = {
  ...resolved,
  status: 'executed',
  confirmed: false,
  verdict: 'unchanged',
  summary: 'The error rate is 17.0% against 18.0% before: no measurable change.',
};

function renderCard(overrides: Partial<RecommendationDto> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <ul>
        <ApprovalCard recommendation={{ ...recommendation, ...overrides }} />
      </ul>
    </QueryClientProvider>,
  );
}

describe('ApprovalCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows what will run before anybody approves it', () => {
    /** Approve means approving that call, and the approval is bound to those exact arguments. */
    renderCard();

    expect(screen.getByText('docker-mcp/update_env_and_restart')).toBeInTheDocument();
    expect(screen.getByText('sentinel-orders')).toBeInTheDocument();
    expect(screen.getByText('MaxPoolSize=200')).toBeInTheDocument();
  });

  it('says what approving is doing while it does it', async () => {
    /** The call runs the action and re-measures; a bare spinner would read as a hung page. */
    approve.mockImplementation(() => new Promise(() => {}));
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Approve and run' }));

    expect(await screen.findByText(/Running and measuring/)).toBeInTheDocument();
    expect(screen.getByText(/waits for the service to settle/)).toBeInTheDocument();
  });

  it('reports a confirmed fix as one', async () => {
    approve.mockResolvedValue(resolved);
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Approve and run' }));

    expect(await screen.findByText(/the symptom is gone/)).toBeInTheDocument();
    expect(screen.getByText('verified')).toBeInTheDocument();
  });

  it('does not let an action that changed nothing look like a success', async () => {
    /** Executed and verified are different outcomes, and the screen has to keep them apart. */
    approve.mockResolvedValue(ranAndDidNothing);
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Approve and run' }));

    expect(await screen.findByText(/ran, and the symptom is still there/)).toBeInTheDocument();
    expect(screen.getByText(/no measurable change/)).toBeInTheDocument();
  });

  it('will not record a refusal with no reason', async () => {
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Reject' }));

    expect(screen.getByRole('button', { name: 'Record the refusal' })).toBeDisabled();
    expect(reject).not.toHaveBeenCalled();
  });

  it('keeps the reason a refusal was given for', async () => {
    reject.mockResolvedValue({
      ...resolved,
      status: 'rejected',
      confirmed: false,
      executed: false,
      verdict: 'rejected',
      summary: 'The pool size is deliberate.',
    });
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await userEvent.type(screen.getByLabelText('Why not'), 'The pool size is deliberate.');
    await userEvent.click(screen.getByRole('button', { name: 'Record the refusal' }));

    await waitFor(() =>
      expect(reject).toHaveBeenCalledWith('rec-1', 'The pool size is deliberate.'),
    );
    // Twice on purpose: the badge on the card, and the verdict on the outcome.
    await waitFor(() => expect(screen.getAllByText('rejected')).toHaveLength(2));
    expect(screen.getByText('The pool size is deliberate.')).toBeInTheDocument();
  });

  it('offers no decision on something already decided', () => {
    renderCard({ status: 'verified' });

    expect(screen.queryByRole('button', { name: 'Approve and run' })).not.toBeInTheDocument();
    expect(screen.getByText('verified')).toBeInTheDocument();
  });

  it('shows why an approval failed rather than swallowing it', async () => {
    approve.mockRejectedValue(new Error('The AI service is unavailable'));
    renderCard();

    await userEvent.click(screen.getByRole('button', { name: 'Approve and run' }));

    expect(await screen.findByText(/AI service is unavailable/)).toBeInTheDocument();
  });
});
