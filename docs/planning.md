# SentinelAI — Planlama ve Yol Haritası

> Autonomous Incident Investigation & Resolution Platform
> Sürüm: 0.1 (Planlama) — 5 Eylül 2026

Bu doküman, kod yazmaya başlamadan önce gereksinim dokümanının 50. maddesinde istenen kararları kesinleştirir ve 12 fazlık geliştirme yol haritasını tanımlar. Her karar için **neden** kısmı verilmiştir; küçük kararlar açıklamasız varsayım olarak geçilmiştir.

---

## 0. Başlamadan Önce: Ortam Gereksinimleri

| Bileşen | Minimum | Önerilen |
|---|---|---|
| RAM | 16 GB | 32 GB |
| GPU | Yok (CPU inference, 3B model) | 8–12 GB VRAM (7B Q4 model) |
| Disk | 40 GB boş | 80 GB boş |
| OS | Linux / macOS / Windows + WSL2 | Linux |

Kurulacak araçlar: Docker Desktop / Engine + Compose v2 (Phase 1 için yalnızca PostgreSQL 17 container'ı; bilmen gereken 4 komut: `docker compose up -d`, `down`, `logs -f`, `ps`), .NET 8 SDK, Node 20 LTS, Python 3.11 + uv (veya pip), Ollama, Git.

Ollama'ya çekilecek modeller (Phase 3'te):
```bash
ollama pull qwen2.5:7b-instruct        # GPU varsa (ana reasoning)
ollama pull qwen2.5:3b-instruct        # CPU-only geliştirme fallback
```
Fine-tune edilen router modeli Phase 8'de GGUF olarak Ollama'ya kendi Modelfile'ı ile eklenecek.

---

## 1. Nihai Sistem Mimarisi

### 1.1 Bileşenler ve Sorumluluklar

```text
┌──────────────────────────────────────────────────────────────────┐
│                     React + TS + Vite Frontend                    │
│      Dashboard · Incidents · Investigation Graph · Evaluation     │
└───────────────┬───────────────────────────────▲──────────────────┘
                │ REST (HTTPS)                  │ SignalR (realtime timeline)
┌───────────────▼───────────────────────────────┴──────────────────┐
│                  ASP.NET Core 8 API  (backend/)                    │
│  Auth · Incidents · Investigations · Approvals · Services · Eval  │
│  Tek "source of truth": PostgreSQL (schema: sentinel)             │
└───────┬───────────────────────────────────────▲──────────────────┘
        │ POST /investigations (start)           │ POST /internal/events (callback)
┌───────▼───────────────────────────────────────┴──────────────────┐
│               Python FastAPI AI Service  (ai-service/)            │
│  Router (fine-tuned) · Agent State Machine · Hybrid RAG           │
│  Root Cause Engine · Critic/Validator · Confidence · Postmortem   │
│  PostgreSQL (schema: rag) + pgvector                               │
└───────┬─────────────────────────────┬────────────────────────────┘
        │ MCP (Streamable HTTP)       │ HTTP
┌───────▼─────────────────────┐  ┌────▼──────────┐
│ MCP Servers (8 container)   │  │ Ollama (host) │
│ logs · metrics · traces     │  │ qwen2.5:7b    │
│ git · database · source     │  │ sentinel-     │
│ docker · testing            │  │   router      │
└───────┬─────────────────────┘  └───────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│ Observability: Loki · Prometheus · Jaeger · OTel Collector · Grafana│
└───────▲──────────────────────────────────────────────────────────┘
        │ OTLP (logs/metrics/traces)
┌───────┴──────────────────────────────────────────────────────────┐
│ Sample Microservices (.NET 8 minimal API)                         │
│ gateway · users · orders · payments · notifications · PostgreSQL  │
│ + Chaos endpoint: 15 hata senaryosu env/endpoint ile tetiklenir   │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 Mimari Kararlar (nedenleriyle)

| Karar | Seçim | Neden |
|---|---|---|
| Log deposu | **Grafana Loki** (+ OTel Collector) | Spec diyagramında OpenSearch var ama teknoloji listesinde yok. Loki tek binary, ~200 MB RAM, Grafana ile native; OpenSearch 2–4 GB RAM ister. Local çalışma hedefiyle uyumlu. Logs-MCP LogQL üzerinden sorgu yapar. İstenirse `ILogStore` abstraction'ı ile OpenSearch'e geçilebilir. |
| Backend ↔ AI service iletişimi | HTTP + **callback (webhook)** | Investigation dakikalar sürebilir; uzun HTTP bağlantısı yerine AI service her adımı backend'e POST eder, backend DB'ye yazar ve SignalR ile frontend'e iter. Basit, izlenebilir, retry edilebilir. Kuyruk (RabbitMQ) portföy için gereksiz karmaşıklık. |
| Tek PostgreSQL, iki şema | `sentinel` (.NET/EF Core) ve `rag` (Python/SQLAlchemy) | Tek container, tek yedek. Her servis kendi şemasının migration'ından sorumlu; çapraz yazma yok. |
| Agent framework | **Kendi küçük state machine'imiz** (LangGraph değil) | Portföyde "agentic AI biliyorum" mesajı için akışın görünür olması gerekir. ~300 satırlık `StateGraph` sınıfı; LangGraph'e geçiş sonradan mümkün. |
| MCP transport | **Streamable HTTP** | Her MCP server ayrı container; stdio compose içinde çalışmaz. Python `mcp` SDK resmi desteği var. |
| Sample servisler | .NET 8 minimal API | Backend ile aynı stack, OTel .NET SDK olgun, Git-MCP'nin "PaymentService.cs:183" gibi kaynak kodu açması demo için ideal. |
| Frontend state | TanStack Query + Zustand | Server state / client state ayrımı; Redux boilerplate'i gereksiz. |
| Auth | Local kullanıcı tablosu + JWT | Spec "basit local yapı" istiyor. Identity Server gereksiz. |

### 1.3 Investigation Akışı (uçtan uca)

```text
1. Frontend  → POST /api/incidents/{id}/investigate
2. Backend   → investigations satırı (status=running) → POST ai-service /investigations
3. AI svc    → Router (fine-tuned) intent + tools
4. AI svc    → Agent state machine adım adım çalışır
             → her adımda POST backend /internal/investigations/{id}/events
             → backend: investigation_steps + evidence tablolarına yazar, SignalR push
5. AI svc    → Root cause + Critic + Confidence → backend'e final payload
6. Backend   → recommendations (status=pending_approval)
7. Kullanıcı → Approve → backend POST ai-service /actions/{id}/execute (approval_token)
8. AI svc    → destructive MCP tool → test MCP → metrics doğrulama
9. AI svc    → postmortem üretir → backend'e gönderir → RAG'e ingest eder
```

---

## 2. Teknoloji Seçimleri (kesinleşmiş)

| Katman | Teknoloji | Versiyon / Not |
|---|---|---|
| Frontend | React 18, TypeScript, Vite 5, Tailwind, TanStack Query, Zustand, Recharts, React Flow (investigation graph), @microsoft/signalr | |
| Backend | ASP.NET Core 8, EF Core 8 + Npgsql, MediatR (CQRS-lite), FluentValidation, Serilog, SignalR, OpenTelemetry .NET | Clean Architecture: Domain / Application / Infrastructure / Api |
| AI Service | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2 + asyncpg, httpx, `mcp` SDK, sentence-transformers, `rank_bm25` | |
| Ana LLM | **Qwen2.5-7B-Instruct** (Ollama, Q4_K_M) | TR+EN, JSON/tool-call uyumu iyi. CPU fallback: Qwen2.5-3B |
| Router modeli | **Qwen2.5-1.5B-Instruct + QLoRA** | 1.5B: 150 ms hedef latency, tek GPU/Colab'da eğitilir. 0.5B alternatif. |
| Embedding | **BAAI/bge-m3** (1024 dim) | Çok dilli (TR/EN aynı vektör uzayı), MIT lisans, dense çıkışı pgvector'a yazılır |
| Reranker | **BAAI/bge-reranker-v2-m3** | Çok dilli cross-encoder, CPU'da 30 aday için <1 sn |
| Vector DB | PostgreSQL 17 + pgvector 0.8 (HNSW index) | Tek DB; Qdrant `IVectorStore` arkasında opsiyonel |
| BM25 | `rank_bm25` (Python, chunk'lar bellekte) + PostgreSQL `tsvector` fallback | Chunk sayısı <100k olduğu sürece bellekte yeterli |
| Fine-tuning | HF Transformers + PEFT + TRL (SFTTrainer), bitsandbytes 4-bit; Unsloth opsiyonel | Export: merge → llama.cpp `convert_hf_to_gguf.py` → Ollama Modelfile |
| Logs | Grafana Loki 3 + OTel Collector | |
| Metrics | Prometheus 2.5x | Servisler `/metrics` expose eder |
| Traces | Jaeger 1.6x (all-in-one), OTLP | |
| Dashboards | Grafana 11 | Provisioned datasources + dashboards |
| Container | Docker Compose v2, çoklu profile (`core`, `observability`, `samples`, `mcp`) | Kademeli geçiş: Phase 1'de sadece Postgres container'da, uygulamalar `dotnet run` / `npm run dev` / `uvicorn` ile native; Phase 2'den itibaren observability katmanı compose'a eklenir |
| Test | xUnit + Testcontainers (.NET), pytest + pytest-asyncio (Python), Vitest (Frontend) | |

---

## 3. Bileşenler Arası Haberleşme Sözleşmeleri

### 3.1 Backend → AI Service
```http
POST /investigations
{
  "investigation_id": "uuid",
  "incident_id": "INC-00142",
  "query": "Payment Service neden hata veriyor? Problemi araştır.",
  "service_hint": "payment-service",
  "callback_url": "http://backend:8080/internal/investigations/{id}/events",
  "callback_token": "..."
}
→ 202 Accepted
```

### 3.2 AI Service → Backend (event callback)
```json
{
  "investigation_id": "uuid",
  "sequence": 7,
  "type": "step_completed | evidence_found | hypothesis | root_cause | recommendation | completed | failed",
  "state": "COLLECT_LOGS",
  "message": "347 NullReferenceException bulundu",
  "payload": { "...": "..." },
  "tool_calls": [{ "tool": "get_service_logs", "latency_ms": 412, "args": {} }],
  "llm_usage": { "model": "qwen2.5:7b", "prompt_tokens": 1820, "completion_tokens": 240, "latency_ms": 3100 },
  "timestamp": "2026-09-05T14:24:11Z"
}
```

### 3.3 Backend → Frontend (SignalR hub `/hubs/investigations`)
- `StepUpdated(investigationId, step)`
- `EvidenceAdded(investigationId, evidence)`
- `InvestigationCompleted(investigationId, result)`
- `ApprovalRequested(recommendationId)`

### 3.4 AI Service → MCP Servers
Streamable HTTP, `http://logs-mcp:7001/mcp` vb. AI service tarafında `McpToolRegistry`: başlangıçta tüm server'lardan `list_tools` çeker, her tool'u `read_only | destructive` olarak etiketler (server tarafında tool annotation `destructiveHint`). Destructive çağrı `approval_token` olmadan reddedilir.

---

## 4. Database Modeli

### 4.1 `sentinel` şeması (Backend / EF Core)

```sql
services            (id, name UNIQUE, display_name, repo_path, health_url, metrics_job, created_at)
users               (id, username UNIQUE, password_hash, role, created_at)

incidents           (id, incident_code UNIQUE 'INC-00142', service_id FK, title, description,
                     severity ENUM(low,medium,high,critical), status ENUM(open,investigating,
                     awaiting_approval,resolving,resolved,closed), started_at, resolved_at,
                     created_by FK users, created_at)

investigations      (id, incident_id FK, query, router_intent, router_output JSONB,
                     status ENUM(queued,running,completed,failed,cancelled),
                     started_at, completed_at, total_duration_ms, llm_calls, tool_calls,
                     prompt_tokens, completion_tokens)

investigation_steps (id, investigation_id FK, sequence, state, message, payload JSONB,
                     duration_ms, started_at, completed_at)

evidence            (id, investigation_id FK, step_id FK, source ENUM(logs,metrics,traces,git,
                     database,source_code,docker,historical_incident,rag_document),
                     summary, raw JSONB, weight NUMERIC(3,2), created_at)

hypotheses          (id, investigation_id FK, title, description, score NUMERIC(3,2),
                     rank, is_selected BOOL)

root_causes         (id, investigation_id FK, hypothesis_id FK, title, category
                     (15 senaryo kategorisi), confidence NUMERIC(3,2), explanation,
                     validator_output JSONB, validator_confidence NUMERIC(3,2))

recommendations     (id, investigation_id FK, root_cause_id FK, action_code, description,
                     tool_name, tool_args JSONB, requires_approval BOOL,
                     status ENUM(pending_approval,approved,rejected,executing,executed,
                     verified,failed), approved_by FK users, approved_at, executed_at,
                     execution_result JSONB, verification_result JSONB)

postmortems         (id, incident_id FK, content_markdown, structured JSONB,
                     duration_minutes, lessons_learned, changed_files JSONB,
                     relevant_commits JSONB, ingested_to_rag BOOL, created_at)

tool_calls          (id, investigation_id FK, step_id FK, server, tool, args JSONB,
                     result_summary, success BOOL, latency_ms, is_destructive BOOL,
                     approval_id FK recommendations NULL, called_at)

model_predictions   (id, investigation_id FK NULL, model_name, purpose ENUM(routing,reasoning,
                     validation,postmortem), prompt_tokens, completion_tokens, latency_ms,
                     valid_json BOOL, output JSONB, called_at)

evaluation_runs     (id, kind ENUM(router,rag,agent), model_or_config, started_at,
                     completed_at, metrics JSONB, notes)
evaluation_results  (id, run_id FK, case_id, expected JSONB, actual JSONB, passed BOOL,
                     details JSONB)
```

İndeksler: `incidents(service_id, status)`, `investigation_steps(investigation_id, sequence)`, `tool_calls(investigation_id)`, `evidence(investigation_id)`.

### 4.2 `rag` şeması (AI Service / SQLAlchemy + Alembic)

```sql
documents        (id, source_type ENUM(incident,runbook,architecture,service_doc,code_doc,
                  postmortem,deployment_doc,known_error), title, service, external_id
                  ('INC-0023'), path, content_hash, metadata JSONB, ingested_at)

document_chunks  (id, document_id FK, chunk_index, content TEXT, token_count,
                  embedding VECTOR(1024), tsv TSVECTOR GENERATED,
                  metadata JSONB  -- {document_type, service, incident_id, date, severity})
                  INDEX hnsw (embedding vector_cosine_ops)
                  INDEX gin (tsv)
                  INDEX gin (metadata jsonb_path_ops)

retrieval_logs   (id, investigation_id, query, filters JSONB, bm25_ids JSONB,
                  vector_ids JSONB, fused_ids JSONB, reranked_ids JSONB,
                  latency_ms JSONB, created_at)
```

---

## 5. MCP Tool Listesi (kesinleşmiş)

| Server | Port | Tool | Tür | Backend |
|---|---|---|---|---|
| logs-mcp | 7001 | `get_service_logs(service, minutes, level?)` | R | Loki LogQL |
| | | `search_logs(query, service?, minutes)` | R | |
| | | `get_recent_errors(service, minutes, limit)` | R | |
| | | `get_exception_statistics(service, minutes)` | R | exception type → count |
| | | `get_error_rate(service, minutes)` | R | log tabanlı |
| metrics-mcp | 7002 | `get_service_metrics(service, minutes)` | R | Prometheus HTTP API |
| | | `get_cpu_usage`, `get_memory_usage`, `get_request_rate`, `get_error_rate`, `get_response_time` (p50/p95/p99) | R | PromQL |
| | | `query_promql(expr, minutes)` | R | serbest sorgu (kısıtlı) |
| traces-mcp | 7003 | `get_recent_traces(service, minutes, limit)` | R | Jaeger API |
| | | `get_failed_traces(service, minutes)` | R | |
| | | `get_trace_details(trace_id)` | R | |
| | | `get_slowest_spans(service, minutes)` | R | |
| | | `get_service_dependencies()` | R | Jaeger dependencies |
| database-mcp | 7004 | `get_database_health()` | R | pg_stat_* |
| | | `get_connection_count()` | R | max_connections vs aktif |
| | | `get_slow_queries(limit)` | R | pg_stat_statements |
| | | `get_locks_and_deadlocks()` | R | pg_locks |
| | | `describe_table(table)` | R | |
| | | `execute_readonly_query(sql)` | R | READ ONLY transaction, statement_timeout, sadece SELECT parse edilir |
| | | `execute_write_query(sql, approval_token)` | **D** | approval zorunlu |
| git-mcp | 7005 | `get_recent_commits(repo, since_minutes?, limit)` | R | GitPython |
| | | `get_commit_diff(repo, sha)`, `get_changed_files(repo, sha)` | R | |
| | | `search_commit_messages(repo, query)`, `get_file_history(repo, path)` | R | |
| | | `get_commits_between(repo, from, to)` | R | deployment korelasyonu |
| | | `revert_commit(repo, sha, approval_token)` | **D** | |
| source-code-mcp | 7006 | `search_code(query, repo?)` | R | ripgrep + embedding |
| | | `read_file(path, start?, end?)`, `find_symbol(name)`, `find_references(symbol)`, `get_project_structure(repo)` | R | |
| | | `apply_patch(path, diff, approval_token)` | **D** | |
| docker-mcp | 7007 | `list_containers()`, `get_container_status(name)`, `get_container_logs(name, tail)`, `get_container_stats(name)` | R | Docker SDK (socket mount) |
| | | `restart_container(name, approval_token)` | **D** | |
| | | `update_env_and_restart(name, env, approval_token)` | **D** | config fix için |
| testing-mcp | 7008 | `run_tests(service)`, `run_specific_test(service, filter)`, `run_service_tests(service)` | R* | `dotnet test`, sonuç TRX parse |
| | | `run_smoke_check(service)` | R | health + birkaç istek |

R = read-only, D = destructive (approval token zorunlu). `*` test koşmak yan etkisiz kabul edildi.

Yaklaşık **40 tool**. Her tool Pydantic input şeması, kısa ve LLM-dostu açıklama, `readOnlyHint / destructiveHint` annotation'ı ile tanımlanır.

---

## 6. RAG Pipeline (kesinleşmiş)

```text
Ingestion:
  docs/ , postmortems , incident kayıtları , runbooks , kod açıklamaları
     → Loader (md/txt/json/code)
     → Chunker (token-based, 400 token, 60 overlap; kod için fonksiyon sınırı)
     → Metadata enrichment {document_type, service, incident_id, date, severity, path}
     → bge-m3 embedding (batch 32, normalize)
     → rag.document_chunks (pgvector + tsvector)
     → BM25 index rebuild (bellek, startup'ta ve ingest sonrası)

Retrieval (HybridRetriever):
  query + filters
     ├─ BM25 top-30  (metadata filter uygulanmış chunk alt kümesi)
     └─ Vector top-30 (pgvector cosine, WHERE metadata @> filters)
     → Reciprocal Rank Fusion (k=60) → top-30 aday
     → bge-reranker-v2-m3 (query, chunk) skorları → top-5
     → context builder (max 3.000 token, kaynak + metadata etiketiyle)
     → LLM
```

Abstraction'lar (`ai-service/rag/`):
- `IEmbeddingProvider` → `BgeM3EmbeddingProvider`
- `IVectorStore` → `PgVectorStore` (Qdrant opsiyonel)
- `ILexicalIndex` → `Bm25Index`
- `IReranker` → `BgeCrossEncoderReranker`
- `IRetriever` → `Bm25Retriever`, `VectorRetriever`, `HybridRetriever`, `HybridRerankRetriever` (evaluation için dördü de aynı arayüzde)

Historical incident benzerlik skoru: incident özet metni embedding'inin geçmiş postmortem embedding'leri ile cosine benzerliği; `> 0.85` ise "INC-0032 ile %91 benzerlik" olarak evidence'a eklenir.

---

## 7. Agent State Machine

```text
State enum:
  UNDERSTAND_INCIDENT → PLAN → COLLECT_LOGS → COLLECT_METRICS → COLLECT_TRACES
  → CHECK_DEPLOYMENTS → SEARCH_HISTORY → INSPECT_CODE → GENERATE_HYPOTHESES
  → COLLECT_ADDITIONAL_EVIDENCE → RANK_HYPOTHESES → SELECT_ROOT_CAUSE
  → VALIDATE (Critic) → RECOMMEND_FIX → END
  Terminal: COMPLETED | FAILED | NEEDS_HUMAN
```

Kurallar:
- `InvestigationContext` tek veri yapısı: `incident, plan, evidence[], hypotheses[], tool_budget, iteration, notes`.
- Her state bir `Node` sınıfı: `run(ctx) -> Transition(next_state, events)`.
- **PLAN** state'i router çıktısına göre hangi COLLECT_* adımlarının atlanacağına karar verir (örneğin `LOG_QUERY` intent'inde sadece loglar).
- **Geri dönüş**: `COLLECT_ADDITIONAL_EVIDENCE` bir hipotez için eksik sinyal tespit ederse `COLLECT_*` veya `INSPECT_CODE`'a geri dönebilir; `max_iterations = 3`, toplam tool çağrısı bütçesi `25`.
- **VALIDATE** başarısız (`valid=false`) ise `GENERATE_HYPOTHESES`'e bir kez geri dönülür; ikinci başarısızlıkta `NEEDS_HUMAN`.
- Confidence `< 0.70` → `NEEDS_HUMAN`, recommendation üretilmez.
- Her transition backend'e event olarak gönderilir (bölüm 3.2).

Confidence formülü (ilk versiyon, açıklanabilir):
```text
confidence = 0.45 * evidence_support        # ağırlıklı evidence oranı (kaynak çeşitliliği bonusu)
           + 0.25 * validator_confidence
           + 0.15 * historical_similarity   # en yakın geçmiş incident benzerliği
           + 0.15 * hypothesis_margin       # 1. ve 2. hipotez skor farkı
```

---

## 8. Fine-Tuning Stratejisi (Router)

**Hedef:** `input → {intent, requires_rag, requires_mcp, tools[], target_service?}` JSON'ı üreten, 150 ms altı, %95+ intent doğruluğuna sahip küçük model.

1. **Dataset üretimi** (`datasets/routing/`)
   - 15 intent × servis × TR/EN kalıp şablonları ile deterministik üretim (~1.500).
   - Qwen2.5-7B (local) ile paraphrase augmentation, her örnek için 3–5 varyant (~2.500).
   - Negatif/belirsiz örnekler: `GENERAL_QUESTION`, çoklu servis, yazım hataları, Türkçe-İngilizce karışık.
   - Deduplication (normalize + fuzzy), şema validasyonu, `train/val/test = 80/10/10` stratified.
   - Hedef: **3.000–4.000** örnek, JSONL.
2. **Eğitim** (`ai-service/training/`)
   - Base: Qwen2.5-1.5B-Instruct. QLoRA r=16, alpha=32, dropout 0.05, target: q/k/v/o/gate/up/down.
   - 3 epoch, lr 2e-4, cosine, batch 8 (grad accum), max_len 512. Chat template + sabit system prompt.
   - Donanım: 8 GB VRAM yeterli; yoksa Google Colab ücretsiz T4 (ücretli servis değil, model local'e indirilir).
3. **Export**: LoRA merge → HF → `convert_hf_to_gguf.py` → Q8_0 → Ollama `Modelfile` (`FROM ./sentinel-router.gguf`, `PARAMETER temperature 0`, JSON format).
4. **Entegrasyon**: `IRouter` → `FineTunedRouter` (Ollama, `format: json`) + `BaseModelRouter` (aynı prompt, base model) + fallback `RuleBasedRouter` (JSON invalid ise).
5. **Evaluation**: test split üzerinde intent accuracy, tool precision/recall/F1 (multi-label), invalid JSON rate, latency p50/p95; base vs fine-tuned tablosu `evaluation_runs`'a yazılır.

---

## 9. Evaluation Stratejisi

| Benchmark | Veri | Metrikler | Nerede |
|---|---|---|---|
| Router | `datasets/routing/test.jsonl` | intent acc, tool P/R/F1, invalid JSON %, latency | `ai-service/evaluation/router_eval.py` |
| RAG | `datasets/evaluation/rag_queries.jsonl` (query → relevant chunk/doc id'leri, ~100 sorgu) | Recall@1/3/5, MRR, Precision@5; 4 retriever karşılaştırması | `rag_eval.py` |
| Agent | `datasets/evaluation/incidents/*.json` (15 senaryo × varyasyon, expected_root_cause, required_tools) | Root cause accuracy, required-tool coverage, false remediation rate, avg duration, avg tool calls | `agent_eval.py` (senaryoyu chaos endpoint ile tetikler, investigation koşar, karşılaştırır) |
| Sistem | Prometheus + `model_predictions` / `tool_calls` | LLM latency, token, tool latency, RAG latency, hata oranı | Grafana + frontend Evaluation sayfası |

Tüm koşular `POST /api/evaluations/run` ile backend üzerinden başlatılır, sonuç `evaluation_runs/results` tablolarına yazılır ve frontend'de grafik olarak gösterilir. CI'da (GitHub Actions) router ve RAG eval'ı hafif modda çalışır; agent eval sadece local.

---

## 10. Final Klasör Yapısı

```text
sentinel-ai/
├── frontend/                      React + TS + Vite
│   └── src/{app,features/{incidents,investigations,services,knowledge,evaluation,models,mcp},components,api,hooks,lib}
├── backend/
│   ├── src/Sentinel.Domain/        Entities, Enums, ValueObjects
│   ├── src/Sentinel.Application/   Commands/Queries (MediatR), Interfaces, DTOs, Validators
│   ├── src/Sentinel.Infrastructure/ EF Core, Repositories, AiServiceClient, Auth, Serilog
│   ├── src/Sentinel.Api/           Controllers, Hubs, Middleware, Program.cs
│   └── tests/{Sentinel.UnitTests,Sentinel.IntegrationTests}
├── ai-service/
│   ├── app/{main.py,config.py,api/,db/}
│   ├── agents/{state_machine.py,context.py,nodes/,planner.py,hypotheses.py,root_cause.py,critic.py,confidence.py,postmortem.py}
│   ├── rag/{ingestion/,chunking.py,embeddings/,stores/,lexical/,rerank/,retrievers/,context_builder.py}
│   ├── llm/{base.py,ollama_provider.py,prompts/,structured.py}
│   ├── routing/{base.py,fine_tuned_router.py,base_model_router.py,rule_router.py,schema.py}
│   ├── mcp_client/{registry.py,client.py,policy.py}
│   ├── evaluation/{router_eval.py,rag_eval.py,agent_eval.py,metrics.py}
│   ├── training/{generate_dataset.py,augment.py,split.py,train_qlora.py,export_gguf.py,Modelfile}
│   ├── observability/{tracing.py,metrics.py}
│   └── tests/
├── mcp-servers/
│   ├── _shared/                    ortak: approval doğrulama, logging, base server
│   ├── logs/ metrics/ traces/ git/ database/ source-code/ docker/ testing/
│   └── tests/
├── sample-services/
│   ├── gateway/ users/ orders/ payments/ notifications/   (.NET 8)
│   ├── _shared/Sentinel.Samples.Common/   OTel setup, chaos middleware
│   └── chaos/scenarios.md          15 senaryo tanımı
├── datasets/
│   ├── routing/{raw,train.jsonl,val.jsonl,test.jsonl}
│   ├── incidents/                  RAG'e ingest edilecek başlangıç geçmiş incident'ları (seed)
│   └── evaluation/{rag_queries.jsonl,incidents/*.json}
├── infrastructure/
│   ├── postgres/{init/*.sql}       PostgreSQL 17, pgvector 0.8 ext, şemalar
│   ├── prometheus/prometheus.yml
│   ├── grafana/{provisioning,dashboards}
│   ├── loki/loki.yml
│   ├── otel-collector/config.yml
│   └── jaeger/
├── models/                         GGUF çıktıları (gitignored), Modelfile'lar
├── scripts/{setup.sh,seed.sh,ingest_docs.sh,run_eval.sh,demo.sh}
├── docs/{architecture.md,adr/,rag.md,mcp.md,agent.md,fine-tuning.md,evaluation.md,demo.md,screenshots/}
├── docker-compose.yml              profiles: core, observability, samples, mcp, all
├── docker-compose.dev.yml
├── .env.example
├── Makefile
└── README.md
```

---

## 11. Yol Haritası (12 Faz)

Süreler part-time (haftada ~15–20 saat) varsayımıyla verilmiştir. Her faz "Done kriteri" sağlanmadan sonrakine geçilmez.

| Faz | Kapsam | Süre | Done kriteri |
|---|---|---|---|
| **1. Project Foundation** | Repo, compose (yalnızca PostgreSQL 17 + pgvector; diğer servisler native çalışır), Postgres (2 şema), 5 sample servis (health + basit iş akışı: user→order→payment→notification), backend skeleton (Clean Arch, EF migration, Services/Incidents CRUD, JWT), frontend skeleton (layout, Services/Incidents sayfaları) | 1.5 hafta | `docker compose --profile core --profile samples up` → dashboard servisleri listeler, incident oluşturulabilir |
| **2. Observability** | OTel SDK tüm servislerde, OTel Collector, Loki, Prometheus, Jaeger, Grafana provisioned dashboard, chaos middleware + ilk 5 senaryo | 1 hafta | Order isteği Jaeger'da 4 servislik trace; Loki'de yapılandırılmış log; Prometheus'ta error_rate |
| **3. Local LLM** | ai-service skeleton, `ILocalLlmProvider` + `OllamaLlmProvider`, structured output (Pydantic + JSON mode + retry), prompt registry, `model_predictions` kaydı | 1 hafta | `POST /llm/structured` ile şemaya uyan JSON döner; latency/token DB'de |
| **4. MCP Infrastructure** | `_shared` base, logs/metrics/traces/git/database/source-code MCP (read-only tool'lar), `McpToolRegistry`, policy katmanı, MCP Tools frontend sayfası | 2 hafta | AI service tüm tool'ları listeler ve gerçek Loki/Prometheus/Jaeger/Git/PG'den veri çeker; her tool için test |
| **5. Hybrid RAG** | Ingestion pipeline, chunker, bge-m3, PgVectorStore, BM25, RRF, metadata filtre, seed dokümanlar (runbook, arch doc, 10 geçmiş incident) | 1.5 hafta | `POST /rag/search` hybrid sonuç döner; filtreli arama çalışır |
| **6. Reranker** | bge-reranker-v2-m3, `HybridRerankRetriever`, context builder, ilk RAG eval (4 retriever) | 0.5 hafta | Recall@5 tablosunda hybrid+rerank en yüksek |
| **7. Investigation Agent** | State machine, node'lar, planner (şimdilik rule-based router), evidence collector, hypothesis/rank/root cause, critic, confidence, event callback, SignalR, timeline + investigation graph + root cause ekranı | 3 hafta | Demo senaryosu (pool 200→20) uçtan uca root cause + evidence üretir |
| **8. Fine-Tuned Router** | Dataset üretimi (3–4k), QLoRA, GGUF export, `FineTunedRouter`, router eval, base vs fine-tuned, Models sayfası | 2 hafta | Intent acc ≥ %95, invalid JSON ≤ %2, agent router'ı kullanır |
| **9. Incident Memory** | Postmortem generator, otomatik RAG ingest, historical similarity evidence, Knowledge Base sayfası | 1 hafta | Çözülen incident bir sonrakinde "INC-xxx ile %N benzerlik" olarak görünür |
| **10. Human-in-the-loop & Remediation** | Approval akışı, approval token, destructive tool'lar (docker/git/db/code), testing-mcp, verify (error rate tekrar ölçüm), docker-mcp | 1.5 hafta | Approve → env fix → restart → test → doğrulama → resolved |
| **11. Evaluation Dashboard** | 15 senaryonun tamamı, agent eval runner, `evaluation_runs`, Evaluation sayfası (benchmark, base vs FT, RAG karşılaştırma, false remediation), Grafana SentinelAI self-observability | 1.5 hafta | Tek komutla tüm benchmarklar koşar, frontend'de grafik |
| **12. Final** | UI polish, README + architecture diagram, docs/, screenshots, `scripts/demo.sh`, CI (build + unit + router/rag eval) | 1 hafta | README'den 20 dakikada kurulup demo çalıştırılabiliyor |

**Toplam:** ~17 hafta part-time. Full-time'da ~8–9 hafta.

### Fazlar arası bağımlılık
```text
1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12
             └───────────┘ (4 ve 5 paralel yürütülebilir)
```

---

## 12. Riskler ve Önlemler

| Risk | Etki | Önlem |
|---|---|---|
| 7B modelin CPU'da yavaş olması | Investigation dakikalar sürer | 3B fallback; tool çıktıları özetlenerek prompt küçültülür; router küçük model |
| LLM'in tool argümanlarında halüsinasyon | Yanlış sorgu, boş sonuç | Pydantic şema + retry; planner kural tabanlı ön-filtre; tool bütçesi |
| Fine-tune datasetinin sentetik ve tekdüze olması | Gerçek sorgularda düşük doğruluk | LLM augmentation + manuel 200 örnek; test setinde elle yazılmış "zor" örnekler |
| Compose'un çok ağır olması (15+ container) | Geliştirme yavaşlar | Profiles; MCP server'ları tek image, farklı entrypoint |
| Chaos senaryolarının deterministik olmaması | Agent eval flakiness | Her senaryo idempotent enable/disable endpoint; eval öncesi reset |
| Scope creep | Bitmeyen proje | Faz done kriterleri; "nice to have" listesi docs/roadmap.md'ye ertelenir |

---

## 13. Faz 1'e Giriş Kontrol Listesi

- [ ] Docker Desktop, .NET 8, Node 20, Python 3.11, Ollama kurulu
- [ ] `docker compose up -d` ile PostgreSQL 17 + pgvector container'ı çalıştırıldı ve bağlantı doğrulandı
- [ ] `ollama pull qwen2.5:7b-instruct` (veya 3b) tamamlandı
- [ ] GitHub repo oluşturuldu (`sentinel-ai`), `.gitignore` (models/, *.gguf, node_modules, bin/obj, .venv)
- [ ] Bu doküman `docs/planning.md` olarak repoya eklendi
- [ ] ADR-001 (Loki vs OpenSearch), ADR-002 (callback vs queue), ADR-003 (custom state machine) `docs/adr/` altında

Bir sonraki adım: **PHASE 1 — Project Foundation** (bu fazda ne yapılacak / neden / hangi dosyalar / faz sonunda sistem ne yapabilecek özetiyle başlanacak).
