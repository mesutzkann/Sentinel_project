import { useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Layout } from '../components/Layout';
import { LoginPage } from '../features/auth/LoginPage';
import { useAuthStore } from '../features/auth/authStore';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { IncidentsPage } from '../features/incidents/IncidentsPage';
import { ServicesPage } from '../features/services/ServicesPage';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Incident data goes stale quickly during an incident, but not so fast that every window
      // focus should refetch. Ten seconds keeps the dashboard current without hammering the API.
      staleTime: 10_000,
      refetchOnWindowFocus: false,

      // A 401 has already cleared the token in the axios interceptor; retrying only delays the
      // redirect to the login page.
      retry: (failureCount, error) =>
        failureCount < 2 && !isUnauthorized(error),
    },
  },
});

function isUnauthorized(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    (error as { response?: { status?: number } }).response?.status === 401
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthGate />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

/**
 * Decides between the login screen and the application shell.
 *
 * The `initializing` flag matters: without it, a stored-but-unverified token would render the
 * login page for a frame before the session is restored.
 */
function AuthGate() {
  const user = useAuthStore((s) => s.user);
  const initializing = useAuthStore((s) => s.initializing);
  const restore = useAuthStore((s) => s.restore);

  useEffect(() => {
    void restore();
  }, [restore]);

  if (initializing) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        Loading...
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="incidents" element={<IncidentsPage />} />
        <Route path="services" element={<ServicesPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
