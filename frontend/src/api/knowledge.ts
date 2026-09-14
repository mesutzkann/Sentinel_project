import { aiApi } from './aiClient';

/**
 * The knowledge base the agent reads, and since Phase 9 also writes.
 *
 * Against the AI service directly, like the MCP and Models endpoints: the `rag` schema belongs
 * to that service, and proxying it through the backend would mean the backend re-declaring a
 * contract it has no part in.
 */

export interface KnowledgeDocument {
  document_id: string;
  title: string;
  source_type: string;
  chunks: number;
  service: string | null;
  external_id: string | null;
  path: string | null;
  ingested_at: string | null;
  /** Written by the investigation agent rather than by a person. */
  generated: boolean;
  metadata: Record<string, string>;
}

export interface KnowledgeDocumentDetail extends KnowledgeDocument {
  content: string;
}

export interface KnowledgeDocuments {
  total: number;
  documents: KnowledgeDocument[];
}

export interface KnowledgeStats {
  documents: number;
  chunks: number;
  documents_by_type: Record<string, number>;
  documents_by_service: Record<string, number>;
  lexical_index_size: number;
  embedding_model: string;
  embedding_available: boolean;
  rerank_model: string;
  rerank_available: boolean;
  rerank_unavailable_reason: string | null;
}

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  title: string;
  source_type: string;
  content: string;
  score: number;
  rank: number;
  service: string | null;
  external_id: string | null;
  path: string | null;
  section: string | null;
}

export interface SearchResponse {
  query: string;
  retriever: string;
  results: SearchHit[];
  latency_ms?: Record<string, number>;
}

export const knowledgeApi = {
  documents: (filters: { sourceType?: string; service?: string } = {}) =>
    aiApi
      .get<KnowledgeDocuments>('/rag/documents', {
        params: {
          source_type: filters.sourceType || undefined,
          for_service: filters.service || undefined,
        },
      })
      .then((r) => r.data),

  document: (id: string) =>
    aiApi.get<KnowledgeDocumentDetail>(`/rag/documents/${id}`).then((r) => r.data),

  stats: () => aiApi.get<KnowledgeStats>('/rag/stats').then((r) => r.data),

  search: (query: string) =>
    aiApi
      .post<SearchResponse>('/rag/search', { query, k: 6, retriever: 'hybrid_rerank' })
      .then((r) => r.data),
};

export const knowledgeQueryKeys = {
  documents: (sourceType?: string, service?: string) =>
    ['knowledge', 'documents', sourceType ?? '', service ?? ''] as const,
  document: (id: string) => ['knowledge', 'document', id] as const,
  stats: ['knowledge', 'stats'] as const,
};
