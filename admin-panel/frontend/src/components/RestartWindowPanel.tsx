import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from './ErrorBanner';
import { toast } from './Toast';

// The automatic restart's repeat window (`alert_remediation`, #501/#558).
// It gates `docker service update --force`, so the server validates strictly:
// a blank field is sent as null and refused, never read as 0 (which would
// mean "restart every time").

export default function RestartWindowPanel() {
  const [cfg, setCfg] = useState<Awaited<ReturnType<typeof api.getAlertRemediation>> | null>(null);
  const [minutes, setMinutes] = useState('');
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);

  function apply(r: NonNullable<typeof cfg>) {
    setCfg(r);
    setMinutes(String(r.repeat_window_minutes));
  }

  useEffect(() => {
    api.getAlertRemediation().then(apply).catch((e: any) => setError(e));
  }, []);

  async function save() {
    setSaving(true); setError(null);
    try {
      const text = minutes.trim();
      apply(await api.saveAlertRemediation(text === '' ? null : Number(text)));
      toast.ok('Restart window saved. The next restart reads it.');
    } catch (e: any) { setError(e); } finally { setSaving(false); }
  }

  return (
    <div>
      <h4 style={{ marginBottom: 4 }}>Automatic restart</h4>
      <p className="meta" style={{ marginTop: 0 }}>
        A swarm service below its replicas gets one forced restart. If the same problem is back
        within this many minutes, it is not restarted again: the task gets the first restart's
        evidence and one card goes out instead. <code>0</code> restarts every time. Default{' '}
        <code>{cfg?.defaults?.repeat_window_minutes ?? 60}</code> minutes, max{' '}
        <code>{cfg?.max_minutes ?? 1440}</code>.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <input type="number" min={0} max={cfg?.max_minutes} step={1} value={minutes}
          style={{ width: 110 }} onChange={e => setMinutes(e.target.value)} />
        <span className="meta">minutes</span>
        <button type="button" className="btn btn-primary" style={{ fontSize: 11 }}
          disabled={saving || !cfg} onClick={save}>
          {saving ? 'Saving…' : 'Save restart window'}
        </button>
      </div>
    </div>
  );
}
