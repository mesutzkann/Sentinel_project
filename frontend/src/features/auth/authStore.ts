import { create } from 'zustand';
import { authApi } from '../../api/endpoints';
import { tokenStorage } from '../../api/client';
import type { CurrentUser } from '../../api/types';

/**
 * Client state only: who is signed in.
 *
 * Server data (services, incidents) belongs to TanStack Query, not here — that split is the
 * reason the app needs both libraries rather than either one alone.
 */
interface AuthState {
  user: CurrentUser | null;

  /** True until the stored token has been checked on first load, so the router does not flash the login page. */
  initializing: boolean;

  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  restore: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  initializing: true,

  login: async (username, password) => {
    const response = await authApi.login(username, password);
    tokenStorage.set(response.access_token);

    set({
      user: {
        user_id: response.user_id,
        username: response.username,
        role: response.role,
      },
    });
  },

  logout: () => {
    tokenStorage.clear();
    set({ user: null });
  },

  restore: async () => {
    if (!tokenStorage.get()) {
      set({ user: null, initializing: false });
      return;
    }

    try {
      set({ user: await authApi.me(), initializing: false });
    } catch {
      // The token is present but rejected — expired, or signed with a key that has since
      // changed. Either way the only useful outcome is a fresh login.
      tokenStorage.clear();
      set({ user: null, initializing: false });
    }
  },
}));
