import { useState } from 'react';
import { clearCredentials, setCredentials } from '../api/client';

// Runtime login: creds are entered here and stored as a base64 token, then
// verified against a real authed endpoint before we let the app render.
export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [user, setUser] = useState('');
  const [pass, setPass] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <form onSubmit={submit} className="card" style={{ width: 320, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <h2 style={{ margin: 0 }}>AEGIS</h2>
        {error && <div style={{ color: '#c0392b', fontSize: '0.85rem' }}>{error}</div>}
        <input
          autoFocus
          placeholder="Username"
          value={user}
          onChange={e => setUser(e.target.value)}
          style={{ padding: '0.4rem 0.6rem' }}
        />
        <input
          type="password"
          placeholder="Password"
          value={pass}
          onChange={e => setPass(e.target.value)}
          style={{ padding: '0.4rem 0.6rem' }}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !user || !pass}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
