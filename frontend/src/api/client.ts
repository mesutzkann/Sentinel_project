import axios, { AxiosError } from 'axios';
import type { ProblemDetails } from './types';

const TOKEN_STORAGE_KEY = 'sentinel.access_token';

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:5080',
  headers: { 'Content-Type': 'application/json' },
});

/**
 * The token lives in localStorage rather than in memory so a page refresh does not log the user
 * out mid-investigation. That trades a little XSS exposure for usability, which is the right
 * call for a local development platform and would not be for a public one.
 */
export const tokenStorage = {
  get: (): string | null => localStorage.getItem(TOKEN_STORAGE_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_STORAGE_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_STORAGE_KEY),
};

api.interceptors.request.use((config) => {
  const token = tokenStorage.get();

  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }

  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ProblemDetails>) => {
    // An expired or rejected token is unrecoverable here; clearing it sends the router to the
    // login page rather than leaving every subsequent request to fail the same way.
    if (error.response?.status === 401) {
      tokenStorage.clear();
    }

    return Promise.reject(error);
  },
);

/**
 * Pulls a message out of a problem response.
 *
 * Validation failures arrive as a field-to-messages map; those are flattened rather than
 * discarded, because "Validation failed" on its own tells the user nothing.
 */
export function describeError(error: unknown): string {
  if (!axios.isAxiosError(error)) {
    return error instanceof Error ? error.message : 'Unexpected error.';
  }

  const problem = (error as AxiosError<ProblemDetails>).response?.data;

  if (problem?.errors) {
    const messages = Object.values(problem.errors).flat();

    if (messages.length > 0) {
      return messages.join(' ');
    }
  }

  return problem?.detail ?? problem?.title ?? error.message;
}
