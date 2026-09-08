import { useState, type SubmitEvent } from 'react';
import { describeError } from '../../api/client';
import { useAuthStore } from './authStore';

export function LoginPage() {
  const login = useAuthStore((s) => s.login);

  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      await login(username, password);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 h-10 w-10 rounded-lg bg-accent/20 ring-1 ring-accent/40">
            <div className="m-[13px] h-3.5 w-3.5 rounded-sm bg-accent" />
          </div>
          <h1 className="text-lg font-semibold text-slate-100">SentinelAI</h1>
          <p className="mt-1 text-sm text-slate-500">Autonomous incident investigation</p>
        </div>

        <form onSubmit={handleSubmit} className="panel space-y-4 p-6">
          <div>
            <label htmlFor="username" className="label">
              Username
            </label>
            <input
              id="username"
              className="field"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
            />
          </div>

          <div>
            <label htmlFor="password" className="label">
              Password
            </label>
            <input
              id="password"
              type="password"
              className="field"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          {error && (
            <p
              role="alert"
              className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm
                         text-state-bad"
            >
              {error}
            </p>
          )}

          <button type="submit" className="btn-primary w-full" disabled={submitting}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>

        <p className="mt-4 text-center text-xs text-slate-600">
          There is no signup: the first admin is seeded from <code>Seed:AdminPassword</code>.
        </p>
      </div>
    </div>
  );
}
