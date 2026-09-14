/**
 * Wire types, mirroring the backend DTOs.
 *
 * The API serialises with snake_case (Program.cs sets SnakeCaseLower), so these interfaces use
 * snake_case too — translating at the boundary would mean two names for every field and a
 * mapping layer to keep in sync.
 */

export type UserRole = 'viewer' | 'engineer' | 'admin';

export type IncidentSeverity = 'low' | 'medium' | 'high' | 'critical';

export type IncidentStatus =
  | 'open'
  | 'investigating'
  | 'awaiting_approval'
  | 'resolving'
  | 'resolved'
  | 'closed';

export interface LoginResponse {
  access_token: string;
  expires_at: string;
  user_id: string;
  username: string;
  role: UserRole;
}

export interface CurrentUser {
  user_id: string;
  username: string;
  role: UserRole;
}

export interface ServiceDto {
  id: string;
  name: string;
  display_name: string;
  repo_path: string | null;
  health_url: string | null;
  metrics_job: string | null;
  open_incident_count: number;
  created_at: string;
}

export interface IncidentDto {
  id: string;
  incident_code: string;
  service_id: string;
  service_name: string;
  title: string;
  description: string | null;
  severity: IncidentSeverity;
  status: IncidentStatus;
  started_at: string;
  resolved_at: string | null;
  created_by_username: string | null;
  created_at: string;
  investigation_count: number;
}

export interface CreateIncidentBody {
  service_id: string;
  title: string;
  description?: string | null;
  severity: IncidentSeverity;
  started_at?: string | null;
}

/** RFC 7807 problem response, as produced by SentinelExceptionHandler. */
export interface ProblemDetails {
  title?: string;
  detail?: string;
  status?: number;
  errors?: Record<string, string[]>;
}

// ---------------------------------------------------------------- investigations ----

/**
 * Nullable fields are *absent* rather than null on the wire: the API is configured with
 * `DefaultIgnoreCondition = WhenWritingNull`. They are typed `| null` here to match the rest of
 * this file, and every consumer checks for a value rather than for `null` specifically.
 */

export type InvestigationStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export type EvidenceSource =
  | 'logs'
  | 'metrics'
  | 'traces'
  | 'git'
  | 'database'
  | 'source_code'
  | 'docker'
  | 'historical_incident'
  | 'rag_document';

export type RecommendationStatus =
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'executing'
  | 'executed'
  | 'verified'
  | 'failed';

export interface InvestigationDto {
  id: string;
  incident_id: string;
  incident_code: string;
  query: string;
  router_intent: string | null;
  status: InvestigationStatus;
  started_at: string;
  completed_at: string | null;
  total_duration_ms: number | null;
  llm_calls: number;
  tool_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** Why it stopped. Present on a completed run that would not stand behind its conclusion. */
  failure_reason: string | null;
  root_cause_title: string | null;
  root_cause_category: string | null;
  confidence: number | null;
  step_count: number;
  evidence_count: number;
}

export interface InvestigationStepDto {
  id: string;
  /** The agent's event number. Monotonic but not contiguous — evidence events consume numbers too. */
  sequence: number;
  state: string;
  message: string;
  payload: Record<string, unknown> | null;
  duration_ms: number | null;
  started_at: string;
  completed_at: string | null;
}

export interface EvidenceDto {
  id: string;
  step_id: string | null;
  source: EvidenceSource;
  summary: string;
  weight: number;
  /** The tool output behind the summary. Arrives with the final payload, not with the event. */
  raw: Record<string, unknown> | null;
  created_at: string;
}

export interface HypothesisDto {
  id: string;
  title: string;
  description: string | null;
  score: number;
  /** 0 means superseded: it was ranked in a round the critic sent back. */
  rank: number;
  is_selected: boolean;
}

export interface RootCauseDto {
  id: string;
  hypothesis_id: string | null;
  title: string;
  category: string;
  confidence: number;
  explanation: string | null;
  validator_output: CriticVerdict | null;
  validator_confidence: number | null;
}

/** What the critic checked and concluded, as `agents/schemas.py` writes it. */
export interface CriticVerdict {
  supporting_evidence: number[];
  contradicting_evidence: number[];
  unsupported_claims: string[];
  concerns: string[];
  alternative: string | null;
  valid: boolean;
  confidence: number;
}

export interface RecommendationDto {
  id: string;
  root_cause_id: string;
  action_code: string;
  description: string;
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
  requires_approval: boolean;
  status: RecommendationStatus;
}

/** What became of a recommendation somebody approved or refused. */
export interface RecommendationOutcomeDto {
  id: string;
  status: RecommendationStatus;
  executed: boolean;
  /** The action ran *and* the symptom measurably improved. `executed` alone is not success. */
  confirmed: boolean;
  verdict: string;
  summary: string;
  error: string | null;
}

export interface ToolCallDto {
  id: string;
  step_id: string | null;
  server: string;
  tool: string;
  result_summary: string | null;
  success: boolean;
  latency_ms: number;
  called_at: string;
}

export interface InvestigationDetailDto {
  investigation: InvestigationDto;
  steps: InvestigationStepDto[];
  evidence: EvidenceDto[];
  hypotheses: HypothesisDto[];
  root_cause: RootCauseDto | null;
  recommendations: RecommendationDto[];
  tool_calls: ToolCallDto[];
}

export interface StartInvestigationBody {
  query?: string | null;
  service_hint?: string | null;
}
