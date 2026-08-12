import { useEffect, useState } from 'react';
import { clearCredentials, setCredentials } from '../api/client';
import Icon from '../components/icons';

// Runtime login: creds are entered here and stored as a base64 token, then
// verified against a real authed endpoint before we let the app render.
export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [user, setUser] = useState('');
  const [pass, setPass] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // AEGIS_AUTH_DISABLED deployments (behind Cloudflare Access) accept
  // credential-less requests — probe once and skip the prompt if so.
  useEffect(() => {
    fetch('/api/agents')
      .then(resp => {
        if (resp.ok) onSuccess();
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setCredentials(user, pass);
    try {
      const resp = await fetch('/api/agents', {
        headers: { Authorization: `Basic ${btoa(`${user}:${pass}`)}` },
      });
      if (resp.ok) {
        onSuccess();
      } else {
        clearCredentials();
        setError(resp.status === 401 ? 'Invalid username or password.' : `Login failed (${resp.status}).`);
      }
    } catch {
      clearCredentials();
      setError('Could not reach the API.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form onSubmit={submit} className="login-card">
        <div className="login-brand">
          <span className="brand-dot"><Icon name="shield" /></span>
          <h1>AEGIS</h1>
          <p>Sign in to the control panel</p>
        </div>
        {error && <div className="login-error">{error}</div>}
        <input
          autoFocus
          placeholder="Username"
          autoComplete="username"
          value={user}
          onChange={e => setUser(e.target.value)}
        />
        <input
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          value={pass}
          onChange={e => setPass(e.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !user || !pass}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
