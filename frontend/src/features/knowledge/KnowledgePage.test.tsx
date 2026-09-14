import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KnowledgePage } from './KnowledgePage';
import {
  knowledgeApi,
  type KnowledgeDocuments,
  type KnowledgeStats,
  type SearchResponse,
} from '../../api/knowledge';

vi.mock('../../api/knowledge', async () => {
  const actual = await vi.importActual<typeof import('../../api/knowledge')>('../../api/knowledge');
  return {
    ...actual,
    knowledgeApi: { documents: vi.fn(), document: vi.fn(), stats: vi.fn(), search: vi.fn() },
  };
});

const documents = vi.mocked(knowledgeApi.documents);
const document = vi.mocked(knowledgeApi.document);
const stats = vi.mocked(knowledgeApi.stats);
const search = vi.mocked(knowledgeApi.search);

/** One document a person wrote and one the agent did, which is the distinction the page makes. */
const corpus: KnowledgeDocuments = {
  total: 2,
  documents: [
    {
      document_id: 'doc-1',
      title: 'INC-00001 — orders timing out after a connection pool change',
      source_type: 'postmortem',
      chunks: 4,
      service: 'orders',
      external_id: 'INC-00001',
      path: 'incidents/INC-00001-orders-pool-exhaustion.md',
      ingested_at: '2026-09-14T09:00:00+00:00',
      generated: false,
      metadata: { scenario: 'DB_CONNECTION_POOL_EXHAUSTION' },
    },
    {
      document_id: 'doc-2',
      title: 'INC-00142 — the connection pool was reduced to 20',
      source_type: 'postmortem',
      chunks: 3,
      service: 'orders',
      external_id: 'INC-00142',
      path: 'postmortems/INC-00142-the-connection-pool.md',
      ingested_at: '2026-09-14T15:40:00+00:00',
      generated: true,
      metadata: { source: 'generated', confidence: '0.78' },
    },
  ],
};

const corpusStats: KnowledgeStats = {
  documents: 28,
  chunks: 94,
  documents_by_type: { postmortem: 10, runbook: 8 },
  documents_by_service: { orders: 8, payments: 7 },
  lexical_index_size: 94,
  embedding_model: 'bge-m3',
  embedding_available: true,
  rerank_model: 'BAAI/bge-reranker-v2-m3',
  rerank_available: true,
  rerank_unavailable_reason: null,
};

const hits: SearchResponse = {
  query: 'connection pool exhausted',
  retriever: 'hybrid_rerank',
  results: [
    {
      chunk_id: 'c-1',
      document_id: 'doc-1',
      title: 'Runbook — connection pool exhaustion',
      source_type: 'runbook',
      content: 'Raise MaxPoolSize back to 200 and restart the service.',
      score: 0.82,
      rank: 1,
      service: 'orders',
      external_id: 'RB-001',
      path: 'runbooks/pool.md',
      section: 'Runbook > Fix',
    },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <KnowledgePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('KnowledgePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    documents.mockResolvedValue(corpus);
    stats.mockResolvedValue(corpusStats);
  });

  it('marks a document the agent wrote for itself', async () => {
    /** It sits next to ten a person wrote, at a confidence and without review. */
    renderPage();

    expect(await screen.findByText(/INC-00142/)).toBeInTheDocument();

    const badges = screen.getAllByText('agent-written');

    expect(badges).toHaveLength(1);
    expect(badges[0].closest('li')).toHaveTextContent('INC-00142');
  });

  it('narrows the corpus by type and by service', async () => {
    renderPage();

    await screen.findByText(/INC-00001/);
    await userEvent.selectOptions(screen.getByLabelText('Filter by type'), 'postmortem');

    await waitFor(() =>
      expect(documents).toHaveBeenCalledWith({ sourceType: 'postmortem', service: '' }),
    );

    await userEvent.selectOptions(screen.getByLabelText('Filter by service'), 'orders');

    await waitFor(() =>
      expect(documents).toHaveBeenCalledWith({ sourceType: 'postmortem', service: 'orders' }),
    );
  });

  it('opens a document and reads it whole', async () => {
    document.mockResolvedValue({
      ...corpus.documents[1],
      content: '## Summary\n\nThe pool was cut from 200 to 20.',
    });
    renderPage();

    await userEvent.click(await screen.findByText(/INC-00142/));

    expect(await screen.findByText(/The pool was cut from 200 to 20/)).toBeInTheDocument();
    expect(document).toHaveBeenCalledWith('doc-2');

    // The metadata a reader needs to judge an agent-written document: how sure it was.
    expect(screen.getByText(/confidence:/)).toBeInTheDocument();
  });

  it('searches with the retrieval the agent uses', async () => {
    search.mockResolvedValue(hits);
    renderPage();

    await screen.findByText(/INC-00001/);
    await userEvent.type(
      screen.getByLabelText('Search the knowledge base'),
      'connection pool exhausted',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText(/Raise MaxPoolSize back to 200/)).toBeInTheDocument();
    expect(search).toHaveBeenCalledWith('connection pool exhausted');
  });

  it('says an empty result is an answer rather than an error', async () => {
    search.mockResolvedValue({ ...hits, results: [] });
    renderPage();

    await screen.findByText(/INC-00001/);
    await userEvent.type(screen.getByLabelText('Search the knowledge base'), 'quantum tunnelling');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText(/the absence of a precedent is evidence/)).toBeInTheDocument();
  });

  it('tells you what to run when the corpus is empty', async () => {
    documents.mockResolvedValue({ total: 0, documents: [] });
    renderPage();

    expect(await screen.findByText(/POST \/rag\/ingest/)).toBeInTheDocument();
  });
});
