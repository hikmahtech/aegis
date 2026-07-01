import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

const API_BASE = import.meta.env.VITE_API_URL || '';

function ScopeBadge({ on, label }: { on: boolean; label: string }) {
  return (
    <span className={`badge ${on ? 'badge-success' : 'badge-neutral'}`} style={{ marginRight: 4 }}>
      {label}{on ? ' ✓' : ' ✗'}
    </span>
  );
}

// Typed editor for an activity's config: one input per field (checkbox / number /
// text), a compact JSON input for nested values (lists/objects), and a raw-JSON
// escape hatch for adding keys or power editing.
function ConfigFields({ value, onChange }: { value: any; onChange: (v: any) => void }) {
  const [raw, setRaw] = useState(false);
  const [rawText, setRawText] = useState('');
  const obj = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const entries = Object.entries(obj);
  const setKey = (k: string, v: any) => onChange({ ...obj, [k]: v });

  if (raw) {
    return (
      <div>
        <textarea value={rawText} rows={6}
          style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
          onChange={e => { setRawText(e.target.value); try { onChange(JSON.parse(e.target.value)); } catch { /* keep typing */ } }} />
        <button type="button" className="btn" style={{ fontSize: 11, marginTop: 4 }}
          onClick={() => setRaw(false)}>← Form view</button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {entries.length === 0 && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>No config.</span>}
      {entries.map(([k, v]) => (
        <label key={k} style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12 }}>
          <span style={{ minWidth: 130, color: 'var(--text-muted)' }}>{k}</span>
          {typeof v === 'boolean' ? (
            <input type="checkbox" checked={v} onChange={e => setKey(k, e.target.checked)} />
          ) : typeof v === 'number' ? (
            <input type="number" value={v} style={{ flex: 1 }}
              onChange={e => setKey(k, e.target.value === '' ? 0 : Number(e.target.value))} />
          ) : typeof v === 'string' ? (
            <input value={v} style={{ flex: 1 }} onChange={e => setKey(k, e.target.value)} />
          ) : (
            <input value={JSON.stringify(v)} style={{ flex: 1, fontFamily: 'monospace', fontSize: 11 }}
              onChange={e => { try { setKey(k, JSON.parse(e.target.value)); } catch { /* keep typing */ } }} />
          )}
        </label>
      ))}
      <button type="button" className="btn" style={{ fontSize: 11, alignSelf: 'flex-start' }}
        onClick={() => { setRawText(JSON.stringify(value ?? {}, null, 2)); setRaw(true); }}>Raw JSON</button>
    </div>
  );
}

function FlowRow({ act, onSaved, onError }: { act: any; onSaved: (m: string) => void; onError: (e: Error) => void }) {
  const [active, setActive] = useState<boolean>(act.active);
  const [cron, setCron] = useState<string>(act.schedule_cron || '');
  const [cfg, setCfg] = useState<any>(act.config ?? {});
  const [saving, setSaving] = useState(false);
  const dirty = active !== act.active || cron !== (act.schedule_cron || '') ||
    JSON.stringify(cfg) !== JSON.stringify(act.config ?? {});

  async function save() {
    setSaving(true);
    try {
      await api.updateActivity(act.slug, { active, schedule_cron: cron, config: cfg });
      onSaved(`Saved ${act.slug} (takes effect within ~5 min)`);
    } catch (e: any) { onError(e); } finally { setSaving(false); }
  }

  return (
    <tr>
      <td style={{ verticalAlign: 'top' }}>
        <strong>{act.slug}</strong><br />
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{act.workflow_type} · {act.agent_id}</span><br />
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>last run: {(act.last_run || '—').toString().slice(0, 16)}</span>
      </td>
      <td style={{ verticalAlign: 'top', textAlign: 'center' }}>
        <input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} />
      </td>
      <td style={{ verticalAlign: 'top' }}>
        <input value={cron} onChange={e => setCron(e.target.value)} style={{ width: 110, fontFamily: 'monospace' }} />
      </td>
      <td style={{ verticalAlign: 'top' }}>
        <ConfigFields value={cfg} onChange={setCfg} />
      </td>
      <td style={{ verticalAlign: 'top' }}>
        <button className="btn" disabled={!dirty || saving} onClick={save}>{saving ? '…' : 'Save'}</button>
      </td>
    </tr>
  );
}

export default function Flows() {
  const [acts, setActs] = useState<any[]>([]);
  const [accounts, setAccounts] = useState<any[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [gClientId, setGClientId] = useState('');
  const [gClientSecret, setGClientSecret] = useState('');
  const [gStatus, setGStatus] = useState<any>(null);
  const [savingG, setSavingG] = useState(false);
  const [newLabel, setNewLabel] = useState('');

  // Kick off Google consent for a brand-new account label. The reauth backend is
  // label-agnostic and writes config/{label}.json on callback, after which the
  // account shows up in the list below automatically.
  function connectAccount() {
    const label = newLabel.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
    if (!label) return;
    window.open(`${API_BASE}/api/admin/gmail/reauth/${label}/initiate`, '_blank', 'noopener');
    setNewLabel('');
    setMsg(`Opened Google consent for "${label}". After you approve, click Reload to see it.`);
  }

  async function load() {
    try {
      setActs(await api.listActivities());
      setAccounts(await api.listGoogleAccounts());
      const gs = await api.getGoogleOauth();
      setGStatus(gs); setGClientId(gs.client_id || '');
    } catch (e: any) { setError(e); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function saveGoogle() {
    setSavingG(true); setMsg(''); setError(null);
    try {
      const body: any = { client_id: gClientId };
      if (gClientSecret) body.client_secret = gClientSecret;
      const r = await api.saveGoogleOauth(body);
      setGStatus(r); setGClientSecret(''); setMsg('Google OAuth client saved.');
    } catch (e: any) { setError(e); } finally { setSavingG(false); }
  }

  return (
    <div>
      <h1 className="page-title">Flows & Integrations</h1>
      <p className="page-subtitle">Configure scheduled flows and Google accounts. Flow edits are durable and reconciled to Temporal within ~5 minutes.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {msg && <p style={{ color: 'var(--success-text)' }}>{msg}</p>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Google OAuth app</h3>
        <p className="page-subtitle">
          Your own Google Cloud OAuth client (Web application) — required to authorize Gmail / Calendar / Drive.
          {gStatus?.configured ? ` Configured (source: ${gStatus.source}).` : ' Not configured.'}
        </p>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={gClientId} onChange={e => setGClientId(e.target.value)}
            placeholder="Client ID (…apps.googleusercontent.com)" />
          <input type="password" value={gClientSecret} onChange={e => setGClientSecret(e.target.value)}
            placeholder={gStatus?.configured ? '•••••••• (set — leave blank to keep)' : 'Client secret'} />
          <button className="btn" disabled={savingG || !gClientId.trim()} onClick={saveGoogle}>Save</button>
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: 0 }}>
          Add <code>{'{your-base-url}/api/admin/gmail/reauth/{label}/callback'}</code> as an authorized redirect URI in your Google app.
        </p>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Google accounts</h3>
        <p className="page-subtitle">Connect a new account or re-authorize an existing one to add scopes (e.g. Drive). Opens Google consent in a new tab.</p>
        <div className="cfg-row" style={{ marginBottom: 12 }}>
          <input
            value={newLabel}
            onChange={e => setNewLabel(e.target.value)}
            placeholder="account label (e.g. work, personal)"
            onKeyDown={e => { if (e.key === 'Enter') connectAccount(); }}
          />
          <button className="btn btn-primary" disabled={!newLabel.trim() || !gStatus?.configured} onClick={connectAccount}>Connect account</button>
          <button className="btn" onClick={load}>Reload</button>
        </div>
        {!gStatus?.configured && (
          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 0 }}>Configure the Google OAuth app above first.</p>
        )}
        <div className="table-scroll"><table style={{ width: '100%' }}>
          <thead><tr><th style={{ textAlign: 'left' }}>Account</th><th>Scopes</th><th></th></tr></thead>
          <tbody>
            {accounts.map(a => (
              <tr key={a.label}>
                <td><strong>{a.label}</strong><br /><span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{a.email}</span></td>
                <td style={{ textAlign: 'center' }}>
                  {a.has_token
                    ? <><ScopeBadge on={a.has_gmail} label="gmail" /><ScopeBadge on={a.has_calendar} label="calendar" /><ScopeBadge on={a.has_drive} label="drive" /></>
                    : <span style={{ color: 'var(--danger-text)' }}>no token</span>}
                </td>
                <td style={{ textAlign: 'center' }}>
                  <a className="btn" href={`${API_BASE}/api/admin/gmail/reauth/${a.label}/initiate`} target="_blank" rel="noreferrer">Re-authorize</a>
                </td>
              </tr>
            ))}
            {accounts.length === 0 && <tr><td colSpan={3}>No Google accounts configured.</td></tr>}
          </tbody>
        </table></div>
      </div>

      <div className="card">
        <h3>Scheduled flows</h3>
        <div className="table-scroll"><table style={{ width: '100%' }}>
          <thead><tr>
            <th style={{ textAlign: 'left' }}>Flow</th><th>Active</th><th>Schedule (cron)</th><th style={{ textAlign: 'left' }}>Config (JSON)</th><th></th>
          </tr></thead>
          <tbody>
            {acts.map(a => (
              <FlowRow key={a.slug} act={a} onSaved={m => { setMsg(m); load(); }} onError={setError} />
            ))}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}
