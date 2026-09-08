import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import { toast } from '../components/Toast';

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
  const [testResult, setTestResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [spend, setSpend] = useState<any>(null);
  const [spendBy, setSpendBy] = useState<'model' | 'purpose' | 'agent_id'>('model');
  const [spendHours, setSpendHours] = useState(24);

  async function loadSpend(hours = spendHours, by = spendBy) {
    try {
      setSpend(await api.llmSpend(hours, by));
    } catch {
      // A spend figure is worth having and never worth failing the page for.
      setSpend(null);
    }
  }

  async function load() {
    try {
      void loadSpend();
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
    setBusy(true); setError(null);
    try {
      await api.saveLlmBackend(body());
      toast.ok('Saved — chat reloaded immediately; restart the worker to apply to flows.');
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

      {/* What it costs. The figures are the LiteLLM proxy's own per-call
          pricing, recorded on every call — AEGIS keeps no price list. */}
      <div className="card" style={{ marginTop: 12 }}>
        <div className="section-header-row">
          <h3 style={{ marginBottom: 0 }}>
            Spend · ${(spend?.total_usd ?? 0).toFixed(4)}
          </h3>
          <div style={{ display: 'flex', gap: 8 }}>
            <select
              value={spendHours}
              onChange={e => { const h = Number(e.target.value); setSpendHours(h); void loadSpend(h, spendBy); }}
            >
              <option value={24}>last 24h</option>
              <option value={168}>last 7 days</option>
              <option value={720}>last 30 days</option>
            </select>
            <select
              value={spendBy}
              onChange={e => { const b = e.target.value as typeof spendBy; setSpendBy(b); void loadSpend(spendHours, b); }}
            >
              <option value="model">by model</option>
              <option value="purpose">by purpose</option>
              <option value="agent_id">by agent</option>
            </select>
          </div>
        </div>
        {!spend && <p className="meta">no spend recorded yet</p>}
        {spend && (
          <>
            {(spend.groups || []).slice(0, 12).map((g: any) => (
              <div
                key={g.key}
                style={{ display: 'flex', justifyContent: 'space-between', gap: 12, fontSize: 13 }}
              >
                <code>{g.key}</code>
                <span className="meta">
                  ${g.usd.toFixed(4)} · {g.calls} calls · {g.tokens.toLocaleString()} tokens
                  {g.unpriced ? ` · ${g.unpriced} unpriced` : ''}
                </span>
              </div>
            ))}
            {spend.unpriced_calls > 0 && (
              <p className="meta" style={{ marginTop: 8 }}>
                {spend.unpriced_calls} call{spend.unpriced_calls === 1 ? '' : 's'} carry no price —
                a backend that is not the LiteLLM proxy, or a call that failed before reaching a
                model. They are counted here, not in the total.
              </p>
            )}
            <p className="meta" style={{ marginTop: 8 }}>
              Priced by the LiteLLM proxy per call, so this covers every model it serves including
              Bedrock. It is AEGIS&apos;s own accounting of what it asked for, not a reconciliation
              of the provider invoice.
            </p>
          </>
        )}
      </div>

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
        {testResult && (
          <p className={testResult.ok ? 'msg-success' : 'msg-error'}>
            {testResult.ok ? `✓ ${testResult.model}: "${testResult.reply}"` : `✗ ${testResult.error}`}
          </p>
        )}
      </div>
    </div>
  );
}
