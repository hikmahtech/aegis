import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type SlackConfigResponse = {
  bot_token_set: boolean;
  app_token_set: boolean;
  channel: string | null;
  configured: boolean;
  source: 'db' | 'env' | 'none';
};

export default function SlackConfig() {
  const [botToken, setBotToken] = useState('');
  const [appToken, setAppToken] = useState('');
  const [channel, setChannel] = useState('');
  const [status, setStatus] = useState<SlackConfigResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const s = await api.getSlackConfig();
      setStatus(s);
      setChannel(s.channel || '');
      setBotToken('');
      setAppToken('');
    } catch (e: any) { setError(e); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function save() {
    setBusy(true); setMsg(''); setError(null);
    try {
      const body: any = {};
      if (botToken) body.bot_token = botToken;
      if (appToken) body.app_token = appToken;
      if (channel) body.channel = channel;
      await api.saveSlackConfig(body);
      setMsg('Saved.');
      await load();
    } catch (e: any) { setError(e); } finally { setBusy(false); }
  }

  return (
    <div>
      <h1 className="page-title">Slack</h1>
      <p className="page-subtitle">
        Optional — connect a Slack app so AEGIS can send and receive messages there.
        If left unconfigured, AEGIS just runs without Slack (comms idles quietly).
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="cfg-row">
          <span className="cfg-label">Bot token</span>
          <input type="password" value={botToken} onChange={e => setBotToken(e.target.value)}
            placeholder={status?.bot_token_set ? '•••••••• (set — leave blank to keep)' : 'not set'} />
        </div>
        <div className="cfg-row">
          <span className="cfg-label">App token</span>
          <input type="password" value={appToken} onChange={e => setAppToken(e.target.value)}
            placeholder={status?.app_token_set ? '•••••••• (set — leave blank to keep)' : 'not set'} />
        </div>
        <div className="cfg-row">
          <span className="cfg-label">Channel</span>
          <input value={channel} onChange={e => setChannel(e.target.value)}
            placeholder="#general (optional)" />
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={busy} onClick={save}>Save</button>
        </div>
        {msg && <p className="msg-success">{msg}</p>}
        {status && (
          status.configured
            ? <p className="msg-success">Connected — Slack enabled (source: {status.source})</p>
            : <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                Not configured — comms is idling (Slack disabled)
              </p>
        )}
      </div>
    </div>
  );
}
