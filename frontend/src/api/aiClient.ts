import axios from 'axios';

/**
 * The AI service, which is a separate origin from the backend.
 *
 * Deliberately not routed through the backend. The MCP endpoints belong to the service that
 * owns the tool registry, and proxying them would mean the backend re-declaring a contract it
 * has no part in — and re-declaring it again in Phase 7 when the agent starts calling the same
 * endpoints itself.
 *
 * No auth interceptor: the AI service is not on the user's credential path. It is reachable
 * only on loopback, and the destructive tools that will need authorisation carry an approval
 * token issued by the backend rather than a user's session.
 */
export const aiApi = axios.create({
  baseURL: import.meta.env.VITE_AI_SERVICE_URL ?? 'http://localhost:8000',
  headers: { 'Content-Type': 'application/json' },
});
