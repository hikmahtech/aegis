import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type Tiers = { fast: string; balanced: string; smart: string };

export default function ModelsProviders() {
  const [provider, setProvider] = useState('custom');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [keySet, setKeySet] = useState(false);
  const [tiers, setTiers] = useState<Tiers>({ fast: '', balanced: '', smart: '' });
  const [presets, setPresets] = useState<Record<string, { label: string; base_url: string }>>({});
  const [source, setSource] = useState('');
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [testResult, setTestResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const b = await api.getLlmBackend();
      setProvider(b.provider || 'custom');
      setBaseUrl(b.base_url || '');
      setTiers({ fast: b.tiers?.fast || '', balanced: b.tiers?.balanced || '', smart: b.tiers?.smart || '' });
      setKeySet(!!b.api_key_set);
      setPresets(b.presets || {});
      setSource(b.source || '');
      setApiKey('');
    } catch (e: any) { setError(e); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  function pickProvider(p: string) {
    setProvider(p);
    const preset = presets[p];
    if (preset && preset.base_url) setBaseUrl(preset.base_url);
  }

  function body() {
    const b: any = { provider, base_url: baseUrl, tiers };
    if (apiKey) b.api_key = apiKey; // write-only: only send when entered
    return b;
  }

  async function save() {
    setBusy(true); setMsg(''); setError(null);
    try {
      await api.saveLlmBackend(body());
      setMsg('Saved — chat reloaded immediately; restart the worker to apply to flows.');
      setApiKey('');
      await load();
    } catch (e: any) { setError(e); } finally { setBusy(false); }
  }

  async function test() {
    setBusy(true); setTestResult(null); setError(null);
    try { setTestResult(await api.testLlmBackend(body())); }
    catch (e: any) { setError(e); } finally { setBusy(false); }
  }

  return (
    <div>
      <h1 className="page-title">Models &amp; Providers</h1>
      <p className="page-subtitle">
        Point AEGIS at your LLM — local (Ollama / LiteLLM) or hosted (Claude / OpenAI / OpenRouter).
        One OpenAI-compatible endpoint + a model per tier. Current source: <code>{source}</code>.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="cfg-row">
          <span className="cfg-label">Provider</span>
          <select value={provider} onChange={e => pickProvider(e.target.value)}>
            {Object.entries(presets).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </select>
        </div>
        <div className="cfg-row">
          <span className="cfg-label">Base URL</span>
          <input value={baseUrl} onChange={e => setBaseUrl(e.target.value)}
            placeholder="https://… (OpenAI-compatible, usually ends in /v1)" />
        </div>
        <div className="cfg-row">
          <span className="cfg-label">API key</span>
          <input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
            placeholder={keySet ? '•••••••• (set — leave blank to keep)' : 'not set (leave blank for local/no-auth)'} />
        </div>
        <fieldset style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 10 }}>
          <legend>Model per tier</legend>
          {(['fast', 'balanced', 'smart'] as const).map(t => (
            <div key={t} className="cfg-row">
              <span className="cfg-label" style={{ textTransform: 'capitalize' }}>{t}</span>
              <input value={tiers[t]}
                onChange={e => setTiers({ ...tiers, [t]: e.target.value })} placeholder={`model for ${t}`} />
            </div>
          ))}
        </fieldset>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn" disabled={busy} onClick={save}>Save</button>
          <button className="btn" disabled={busy} onClick={test}>Test connection</button>
        </div>
        {msg && <p className="msg-success">{msg}</p>}
        {testResult && (
          <p className={testResult.ok ? 'msg-success' : 'msg-error'}>
            {testResult.ok ? `✓ ${testResult.model}: "${testResult.reply}"` : `✗ ${testResult.error}`}
          </p>
        )}
      </div>
    </div>
  );
}
