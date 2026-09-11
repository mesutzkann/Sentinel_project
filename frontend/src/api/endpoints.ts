import { api } from './client';
import type {
  CreateIncidentBody,
  CurrentUser,
  IncidentDto,
  IncidentStatus,
  InvestigationDetailDto,
  InvestigationDto,
  LoginResponse,
  ServiceDto,
  StartInvestigationBody,
} from './types';

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>('/api/auth/login', { username, password }).then((r) => r.data),

  me: () => api.get<CurrentUser>('/api/auth/me').then((r) => r.data),
};

export const servicesApi = {
  list: () => api.get<ServiceDto[]>('/api/services').then((r) => r.data),

  get: (id: string) => api.get<ServiceDto>(`/api/services/${id}`).then((r) => r.data),
};

export interface IncidentFilters {
  serviceId?: string;
  status?: IncidentStatus;
  activeOnly?: boolean;
  limit?: number;
}

export const incidentsApi = {
  list: (filters: IncidentFilters = {}) =>
    api
      .get<IncidentDto[]>('/api/incidents', {
        params: {
          serviceId: filters.serviceId,
          status: filters.status,
          activeOnly: filters.activeOnly,
          limit: filters.limit,
        },
      })
      .then((r) => r.data),

  get: (id: string) => api.get<IncidentDto>(`/api/incidents/${id}`).then((r) => r.data),

  create: (body: CreateIncidentBody) =>
    api.post<IncidentDto>('/api/incidents', body).then((r) => r.data),

  updateStatus: (id: string, status: IncidentStatus) =>
    api.patch<IncidentDto>(`/api/incidents/${id}/status`, { status }).then((r) => r.data),
};

export const investigationsApi = {
  list: (incidentId?: string, limit = 50) =>
    api
      .get<InvestigationDto[]>('/api/investigations', { params: { incidentId, limit } })
      .then((r) => r.data),

  /**
   * One investigation with everything it produced.
   *
   * One request rather than six, because the parts are only meaningful together: a piece of
   * evidence is an assertion until you can see which step found it and which conclusion cited it.
   */
  get: (id: string) =>
    api.get<InvestigationDetailDto>(`/api/investigations/${id}`).then((r) => r.data),

  /**
   * Hands an incident to the agent. Answers 202 and a row, not a conclusion — that arrives over
   * the hub and over this endpoint's own `get` for the next several minutes.
   */
  start: (incidentId: string, body: StartInvestigationBody = {}) =>
    api
      .post<InvestigationDto>(`/api/incidents/${incidentId}/investigate`, body)
      .then((r) => r.data),
};

/**
 * Query keys in one place so a mutation can invalidate exactly what it affected.
 * Hierarchical: invalidating `incidents.all` also invalidates every filtered list under it.
 */
export const queryKeys = {
  services: {
    all: ['services'] as const,
    detail: (id: string) => ['services', id] as const,
  },
  incidents: {
    all: ['incidents'] as const,
    list: (filters: IncidentFilters) => ['incidents', 'list', filters] as const,
    detail: (id: string) => ['incidents', id] as const,
  },
  investigations: {
    all: ['investigations'] as const,
    list: (incidentId?: string) => ['investigations', 'list', incidentId ?? 'all'] as const,
    detail: (id: string) => ['investigations', id] as const,
  },
  auth: {
    me: ['auth', 'me'] as const,
  },
};
