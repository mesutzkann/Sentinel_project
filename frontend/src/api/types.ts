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
