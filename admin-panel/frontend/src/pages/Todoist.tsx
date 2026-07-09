import { Fragment, useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type TodoistState = {
  sync: { key: string; last_full_sync_at: string | null; last_incremental_at: string | null } | null;
  outbox: {
    counts: Record<string, number>;
    oldest_pending_age_seconds: number | null;
    failed_recent: any[];
  };
  tasks: { open: number; completed_7d: number; pending_clarify: number };
  managed_projects: Record<string, string> | null;
};

type TodoistProject = {
  id: string;
  name: string;
  parent_id?: string | null;
  is_managed?: boolean;
  is_archived?: boolean;
  order_idx?: number;
};

type TodoistLabel = {
  id: string;
  name: string;
  color?: string | null;
  raw?: any;
};

type TodoistTask = {
  id: string;
  content: string;
  description?: string | null;
  project_id: string;
  due_date?: string | null;
  priority?: number | null;
  labels?: string[] | null;
  is_completed?: boolean;
  assignee_label?: string | null;
  source_tag?: string | null;
  last_clarified_at?: string | null;
};

type ClarifyLogEntry = {
  id: string;
  todoist_task_id: string;
  pass?: string | null;
  source_tag?: string | null;
  classification?: string | null;
  confidence?: number | null;
  assignee?: string | null;
  contexts?: string[] | null;
  reason?: string | null;
  user_hint?: string | null;
  llm_model?: string | null;
  applied?: boolean;
  created_at?: string | null;
  task_content?: string | null;
};

function fmtAge(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 120) return `${seconds}s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

function fmtConfidence(c: number | null | undefined): string {
  if (c == null) return '—';
  const pct = c <= 1 ? c * 100 : c;
  return `${Math.round(pct)}%`;
}

function truncate(s: string, n: number): string {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

// AEGIS only manages the native Inbox project as a GTD bucket now — Next/Someday
// moved to @next/@someday labels, and Areas/work-streams are the user's own
// nested Todoist projects (parent_id links child work-stream -> parent area).
const _PROJECT_KEYS = ['inbox'] as const;

// Label grouping for the read-only Labels panel — matches the GTD label scheme
// documented in CLAUDE.md (personalities, GTD state, contexts, everything else).
const _LABEL_GROUPS: { title: string; names: string[] }[] = [
  { title: 'Personalities / assignees', names: ['@me', '@sebas', '@raphael', '@maou', '@pandora'] },
  { title: 'GTD state', names: ['@next', '@someday', '@waiting', '@reference'] },
  { title: 'Contexts', names: ['@5min', '@deep', '@code', '@email', '@phone', '@errand', '@reading', '@home', '@office'] },
];

function groupLabels(labels: TodoistLabel[]): { title: string; labels: TodoistLabel[] }[] {
  const used = new Set<string>();
  const groups = _LABEL_GROUPS.map(g => {
    const names = new Set(g.names);
    const matched = labels.filter(l => names.has(l.name));
    matched.forEach(l => used.add(l.id));
    return { title: g.title, labels: matched };
  });
  const other = labels.filter(l => !used.has(l.id));
  return [...groups, { title: 'Other', labels: other }].filter(g => g.labels.length > 0);
}

// Nest projects by parent_id — top-level Areas with their child work-streams indented.
function buildProjectTree(projects: TodoistProject[]): { project: TodoistProject; children: TodoistProject[] }[] {
  const byParent = new Map<string, TodoistProject[]>();
  const roots: TodoistProject[] = [];
  for (const p of projects) {
    if (p.parent_id) {
      const list = byParent.get(p.parent_id) || [];
      list.push(p);
      byParent.set(p.parent_id, list);
    } else {
      roots.push(p);
    }
  }
  return roots.map(project => ({ project, children: byParent.get(project.id) || [] }));
}

export default function Todoist() {
  const [data, setData] = useState<TodoistState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [cfg, setCfg] = useState<any>(null);
  const [apiKey, setApiKey] = useState('');
  const [projects, setProjects] = useState<Record<string, string>>({ inbox: '' });
  const [savingCfg, setSavingCfg] = useState(false);
  const [cfgMsg, setCfgMsg] = useState('');
  const [gtd, setGtd] = useState<any>(null);
  const [savingGtd, setSavingGtd] = useState(false);
  const [gtdMsg, setGtdMsg] = useState('');

  // Content routes (regex/prefix/contains on task title → assignee/labels/gate)
  const [routes, setRoutes] = useState<any[]>([]);
  const [matchModes, setMatchModes] = useState<string[]>(['prefix', 'contains', 'regex']);
  const [savingRoutes, setSavingRoutes] = useState(false);
  const [routesMsg, setRoutesMsg] = useState('');
  const [previews, setPreviews] = useState<Record<number, any>>({});
  const [suggestExamples, setSuggestExamples] = useState('');
  const [suggesting, setSuggesting] = useState(false);

  // Project picker (shared by Configure + Tasks filter bar)
  const [allProjects, setAllProjects] = useState<TodoistProject[]>([]);
  // Read-only structure panels — projects tree (Areas -> work-streams) + labels.
  const [labels, setLabels] = useState<TodoistLabel[]>([]);
  const [labelsLoading, setLabelsLoading] = useState(true);

  // Tasks workbench
  const [tasks, setTasks] = useState<TodoistTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [taskProjectFilter, setTaskProjectFilter] = useState('');
  const [taskStatusFilter, setTaskStatusFilter] = useState<'open' | 'completed'>('open');
  const [taskAssigneeFilter, setTaskAssigneeFilter] = useState('');
  const [reclarifyBusy, setReclarifyBusy] = useState<string | null>(null);

  // Clarify decisions log
  const [clarifyLog, setClarifyLog] = useState<ClarifyLogEntry[]>([]);
  const [clarifyLoading, setClarifyLoading] = useState(true);
  const [appliedFilter, setAppliedFilter] = useState<'' | 'true' | 'false'>('');
  const [expandedReasons, setExpandedReasons] = useState<Set<string>>(new Set());

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.todoistState());
      const c = await api.getTodoistConfig();
      setCfg(c);
      setProjects({ inbox: '', ...(c.projects || {}) });
      setApiKey('');
      setGtd(await api.getGtdRules());
      const cr = await api.getContentRoutes();
      setRoutes(cr.routes || []);
      setMatchModes(cr.match_modes || ['prefix', 'contains', 'regex']);
      setPreviews({});
      const p = await api.todoistProjects();
      setAllProjects(p || []);
    } catch (e: any) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  async function loadLabels() {
    setLabelsLoading(true);
    try {
      const l = await api.todoistLabels();
      setLabels(l || []);
    } catch (e: any) {
      setError(e);
    } finally {
      setLabelsLoading(false);
    }
  }

  async function loadTasks() {
    setTasksLoading(true);
    try {
      const t = await api.todoistTasks({
        project_id: taskProjectFilter || undefined,
        status: taskStatusFilter || undefined,
        assignee: taskAssigneeFilter || undefined,
        limit: 100,
      });
      setTasks(t || []);
    } catch (e: any) {
      setError(e);
    } finally {
      setTasksLoading(false);
    }
  }

  async function loadClarifyLog() {
    setClarifyLoading(true);
    try {
      const r = await api.todoistClarifyLog({
        limit: 50,
        applied: appliedFilter === '' ? undefined : appliedFilter === 'true',
      });
      setClarifyLog(r || []);
    } catch (e: any) {
      setError(e);
    } finally {
      setClarifyLoading(false);
    }
  }

  async function saveGtd() {
    setSavingGtd(true); setGtdMsg(''); setError(null);
    try {
      const skip: Record<string, string> = {};
      for (const [k, v] of Object.entries(gtd.skip_inbox || {})) if (v) skip[k] = v as string;
      const r = await api.saveGtdRules({ assignee: gtd.assignee, contexts: gtd.contexts, skip_inbox: skip });
      setGtd(r);
      setGtdMsg('Saved — applies within ~30s.');
    } catch (e: any) { setError(e); } finally { setSavingGtd(false); }
  }

  function updateRoute(i: number, patch: any) {
    setRoutes(rs => rs.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function addRoute() {
    setRoutes(rs => [...rs, {
      key: '', match: 'prefix', value: '', assignee: '@pandora', contexts: [],
      area_label: null, gate: true, service: null, resource_tags: [],
    }]);
  }
  function removeRoute(i: number) {
    setRoutes(rs => rs.filter((_, idx) => idx !== i));
    setPreviews(prev => { const n = { ...prev }; delete n[i]; return n; });
  }
  function moveRoute(i: number, dir: number) {
    setRoutes(rs => {
      const j = i + dir;
      if (j < 0 || j >= rs.length) return rs;
      const n = [...rs];
      [n[i], n[j]] = [n[j], n[i]];
      return n;
    });
    setPreviews({});
  }
  async function saveRoutes() {
    setSavingRoutes(true); setRoutesMsg(''); setError(null);
    try {
      const r = await api.saveContentRoutes({ routes });
      setRoutes(r.routes || []);
      setRoutesMsg('Saved — applies within ~30s.');
    } catch (e: any) { setError(e); } finally { setSavingRoutes(false); }
  }
  async function previewRoute(i: number) {
    const r = routes[i];
    try {
      const p = await api.previewContentRoute({ match: r.match, value: r.value });
      setPreviews(prev => ({ ...prev, [i]: p }));
    } catch (e: any) {
      setPreviews(prev => ({ ...prev, [i]: { error: e?.message || String(e) } }));
    }
  }
  async function suggestPattern() {
    const examples = suggestExamples.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    if (!examples.length) return;
    setSuggesting(true); setRoutesMsg(''); setError(null);
    try {
      const s = await api.suggestContentRoute({ examples });
      if (s.pattern) {
        // Never auto-applied — add a new editable row pre-filled with the draft
        // (regex mode). The user edits + previews before Save.
        setRoutes(rs => [...rs, {
          key: '', match: 'regex', value: s.pattern, assignee: '@pandora', contexts: [],
          area_label: null, gate: true, service: null, resource_tags: [],
        }]);
        setRoutesMsg(s.all_examples_match
          ? '✨ Added a suggested pattern matching all examples — review + preview before saving.'
          : '✨ Added a suggested pattern (not all examples matched) — edit + preview before saving.');
      } else {
        setRoutesMsg('Suggest failed: ' + (s.error || 'no pattern derived'));
      }
    } catch (e: any) { setError(e); } finally { setSuggesting(false); }
  }

  async function saveConfig() {
    setSavingCfg(true); setCfgMsg(''); setError(null);
    try {
      const body: any = { projects };
      if (apiKey) body.api_key = apiKey;
      const c = await api.saveTodoistConfig(body);
      setCfg(c); setApiKey('');
      setCfgMsg('Saved — restart the worker to apply a new API key to flows.');
    } catch (e: any) { setError(e); } finally { setSavingCfg(false); }
  }

  async function reclarify(taskId: string) {
    setReclarifyBusy(taskId);
    setError(null);
    try {
      await api.todoistReclarify(taskId);
      await loadTasks();
    } catch (e: any) {
      setError(e);
    } finally {
      setReclarifyBusy(null);
    }
  }

  function toggleReason(id: string) {
    setExpandedReasons(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  useEffect(() => { void load(); void loadLabels(); }, []);
  useEffect(() => { void loadTasks(); }, [taskProjectFilter, taskStatusFilter, taskAssigneeFilter]);
  useEffect(() => { void loadClarifyLog(); }, [appliedFilter]);

  if (loading && !data) return <div className="loading">Loading Todoist state…</div>;

  const failedCount = data?.outbox?.counts?.failed ?? 0;
  const pendingCount = data?.outbox?.counts?.pending ?? 0;

  const projectsById: Record<string, string> = {};
  for (const p of allProjects) projectsById[p.id] = p.name;

  // Picker options for a Configure bucket — always includes the current value
  // even if it's a stale/unknown id, so saving doesn't silently wipe it.
  function bucketOptions(currentId: string): TodoistProject[] {
    if (currentId && !allProjects.some(p => p.id === currentId)) {
      return [{ id: currentId, name: `${currentId} (unknown project)` }, ...allProjects];
    }
    return allProjects;
  }

  return (
    <div>
      <h1 className="page-title">Todoist</h1>
      <p className="page-subtitle">GTD workbench — configure buckets, triage tasks, review clarify decisions, and watch sync health.</p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Configure Todoist</h3>
        <p className="page-subtitle">
          Your Todoist API token + the native Inbox project AEGIS adopts as its one managed GTD bucket.
          {cfg ? ` API key: ${cfg.api_key_set ? `set (${cfg.source})` : 'not set'}.` : ''}
        </p>
        <p className="meta" style={{ marginBottom: 8 }}>
          Next/Someday are no longer AEGIS-managed projects — they're the <code>@next</code> / <code>@someday</code> labels.
          Areas and work-streams are your own nested Todoist projects (see the Projects tree below), not something AEGIS picks here.
        </p>
        <div className="cfg-row">
          <span className="cfg-label">API token</span>
          <input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
            placeholder={cfg?.api_key_set ? '•••••••• (set — leave blank to keep)' : 'Todoist API token'} />
        </div>
        {_PROJECT_KEYS.map(k => (
          <div key={k} className="cfg-row">
            <span className="cfg-label">Inbox project</span>
            <select value={projects[k] || ''} onChange={e => setProjects({ ...projects, [k]: e.target.value })}>
              <option value="">— none —</option>
              {bucketOptions(projects[k] || '').map(p => (
                <option key={p.id} value={p.id}>{p.name}{p.is_archived ? ' (archived)' : ''}</option>
              ))}
            </select>
          </div>
        ))}
        <button className="btn" disabled={savingCfg} onClick={saveConfig}>{savingCfg ? 'Saving…' : 'Save Todoist config'}</button>
        {cfgMsg && <span className="msg-success" style={{ marginLeft: 10 }}>{cfgMsg}</span>}
      </div>

      {/* Projects tree — read-only view of the real Todoist structure (Areas -> work-streams) */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Projects</h3>
        <p className="page-subtitle">
          Areas / work-streams are your own nested Todoist projects — AEGIS just reads this structure, it doesn't manage it.
        </p>
        {allProjects.length === 0 && <div className="empty">No projects found.</div>}
        {allProjects.length > 0 && (
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {buildProjectTree(allProjects).map(({ project, children }) => (
              <li key={project.id} style={{ marginBottom: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <strong>{project.name}</strong>
                  {project.is_managed && <span className="badge badge-success">managed</span>}
                  {project.is_archived && <span className="badge badge-neutral">archived</span>}
                </div>
                {children.length > 0 && (
                  <ul style={{ listStyle: 'none', margin: '4px 0 0 20px', padding: 0, borderLeft: '2px solid var(--border, #e5e5e5)' }}>
                    {children.map(child => (
                      <li key={child.id} style={{ padding: '2px 0 2px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
                        {child.name}
                        {child.is_managed && <span className="badge badge-success">managed</span>}
                        {child.is_archived && <span className="badge badge-neutral">archived</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Labels — read-only view of the label scheme (assignees, GTD state, contexts, other) */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Labels</h3>
        <p className="page-subtitle">Read-only — Next/Someday state and GTD contexts live here now, not as projects.</p>
        {labelsLoading && <div className="loading">Loading labels…</div>}
        {!labelsLoading && labels.length === 0 && <div className="empty">No labels found.</div>}
        {!labelsLoading && labels.length > 0 && groupLabels(labels).map(g => (
          <div key={g.title} style={{ marginBottom: 10 }}>
            <div className="cfg-label" style={{ marginBottom: 4 }}>{g.title}</div>
            <div className="meta-tags-row">
              {g.labels.map(l => <span key={l.id} className="meta-tag">{l.name}</span>)}
            </div>
          </div>
        ))}
      </div>

      {gtd && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>GTD clarify rules</h3>
          <p className="page-subtitle">
            How captured items are auto-labelled by source tag — assignee, context labels, and
            skip-inbox routing. (The @sebas/@raphael/@maou/@pandora agent routing stays in code.)
          </p>
          <div className="table-scroll">
          <table style={{ width: '100%', fontSize: 13 }}>
            <thead><tr>
              <th style={{ textAlign: 'left' }}>Source</th><th>Assignee</th>
              <th>Contexts (comma-sep)</th><th>Skip-inbox →</th>
            </tr></thead>
            <tbody>
              {(gtd.source_tags || []).map((tag: string) => (
                <tr key={tag}>
                  <td><code>{tag}</code></td>
                  <td><input style={{ width: 90 }} value={gtd.assignee?.[tag] || ''}
                    onChange={e => setGtd({ ...gtd, assignee: { ...gtd.assignee, [tag]: e.target.value } })} /></td>
                  <td><input style={{ width: '100%' }} value={(gtd.contexts?.[tag] || []).join(', ')}
                    onChange={e => setGtd({ ...gtd, contexts: { ...gtd.contexts, [tag]: e.target.value.split(',').map(s => s.trim()).filter(Boolean) } })} /></td>
                  <td><input style={{ width: 90 }} placeholder="(none)" value={gtd.skip_inbox?.[tag] || ''}
                    onChange={e => setGtd({ ...gtd, skip_inbox: { ...gtd.skip_inbox, [tag]: e.target.value } })} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <button className="btn" style={{ marginTop: 8 }} disabled={savingGtd} onClick={saveGtd}>
            {savingGtd ? 'Saving…' : 'Save GTD rules'}
          </button>
          {gtdMsg && <span className="msg-success" style={{ marginLeft: 10 }}>{gtdMsg}</span>}
        </div>
      )}

      {/* Content routes */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Content routes</h3>
        <p className="page-subtitle">
          Route Inbox tasks by their <em>title</em> (complements GTD rules, which route by source
          tag). Ordered — first match wins. <strong>gate</strong> on → asks via a Slack card
          ("investigate / I've got it") before an agent runs; <strong>gate</strong> off → applies
          the assignee + contexts directly. Ships empty; add your own rows.
        </p>

        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 12, flexWrap: 'wrap' }}>
          <textarea
            style={{ minWidth: 280, flex: 1, minHeight: 44, fontSize: 13 }}
            placeholder="✨ Suggest a pattern — paste example titles (one per line), e.g.&#10;APP-1234: something broke&#10;BUG-42: crash"
            value={suggestExamples}
            onChange={e => setSuggestExamples(e.target.value)}
          />
          <button className="btn" disabled={suggesting || !suggestExamples.trim()} onClick={suggestPattern}>
            {suggesting ? 'Suggesting…' : '✨ Suggest'}
          </button>
        </div>

        <div className="table-scroll">
        <table style={{ width: '100%', fontSize: 13 }}>
          <thead><tr>
            <th style={{ textAlign: 'left' }}>Order</th>
            <th>Key</th><th>Match</th><th>Value</th><th>Assignee</th>
            <th>Contexts</th><th>Gate</th><th>Area</th><th>Service</th><th>Res. tags</th><th></th>
          </tr></thead>
          <tbody>
            {routes.map((r: any, i: number) => (
              <Fragment key={`route${i}`}>
                <tr>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="btn btn-sm" disabled={i === 0} onClick={() => moveRoute(i, -1)}>↑</button>
                    <button className="btn btn-sm" disabled={i === routes.length - 1} onClick={() => moveRoute(i, 1)}>↓</button>
                  </td>
                  <td><input style={{ width: 90 }} value={r.key || ''}
                    onChange={e => updateRoute(i, { key: e.target.value })} /></td>
                  <td><select value={r.match} onChange={e => updateRoute(i, { match: e.target.value })}>
                    {matchModes.map(m => <option key={m} value={m}>{m}</option>)}
                  </select></td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <input style={{ width: 160 }} value={r.value || ''}
                      onChange={e => updateRoute(i, { value: e.target.value })} />
                    <button className="btn btn-sm" style={{ marginLeft: 4 }} onClick={() => previewRoute(i)}>Preview</button>
                  </td>
                  <td><input style={{ width: 80 }} value={r.assignee || ''}
                    onChange={e => updateRoute(i, { assignee: e.target.value })} /></td>
                  <td><input style={{ width: 110 }} value={(r.contexts || []).join(', ')}
                    onChange={e => updateRoute(i, { contexts: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean) })} /></td>
                  <td style={{ textAlign: 'center' }}><input type="checkbox" checked={!!r.gate}
                    onChange={e => updateRoute(i, { gate: e.target.checked })} /></td>
                  <td><input style={{ width: 90 }} placeholder="(none)" value={r.area_label || ''}
                    onChange={e => updateRoute(i, { area_label: e.target.value })} /></td>
                  <td><input style={{ width: 70 }} placeholder="(none)" value={r.service || ''}
                    onChange={e => updateRoute(i, { service: e.target.value })} /></td>
                  <td><input style={{ width: 90 }} value={(r.resource_tags || []).join(', ')}
                    onChange={e => updateRoute(i, { resource_tags: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean) })} /></td>
                  <td><button className="btn btn-sm" onClick={() => removeRoute(i)}>✕</button></td>
                </tr>
                {previews[i] && (
                  <tr key={`p${i}`}>
                    <td colSpan={11} style={{ fontSize: 12, background: 'var(--bg-subtle, #f6f6f6)' }}>
                      {previews[i].error
                        ? <span className="msg-error">Preview error: {previews[i].error}</span>
                        : <span>
                            Matches <strong>{previews[i].match_count}</strong> of your current Inbox tasks
                            {previews[i].matches?.length > 0 && (
                              <>: <code>{previews[i].matches.slice(0, 5).join('  ·  ')}</code>
                                {previews[i].match_count > 5 && <em> …and {previews[i].match_count - 5} more</em>}</>
                            )}
                          </span>}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {routes.length === 0 && (
              <tr><td colSpan={11} style={{ color: 'var(--text-muted, #888)' }}>No routes — add one, or ✨ suggest from examples.</td></tr>
            )}
          </tbody>
        </table>
        </div>
        <button className="btn" style={{ marginTop: 8 }} onClick={addRoute}>+ Add route</button>
        <button className="btn btn-primary" style={{ marginTop: 8, marginLeft: 8 }} disabled={savingRoutes} onClick={saveRoutes}>
          {savingRoutes ? 'Saving…' : 'Save content routes'}
        </button>
        {routesMsg && <span className="msg-success" style={{ marginLeft: 10 }}>{routesMsg}</span>}
      </div>

      {/* Tasks workbench */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>Tasks</h2>
        <div className="card">
          <div className="filter-bar">
            <select value={taskProjectFilter} onChange={e => setTaskProjectFilter(e.target.value)}>
              <option value="">All projects</option>
              {allProjects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <select value={taskStatusFilter} onChange={e => setTaskStatusFilter(e.target.value as 'open' | 'completed')}>
              <option value="open">Open</option>
              <option value="completed">Completed</option>
            </select>
            <input
              value={taskAssigneeFilter}
              onChange={e => setTaskAssigneeFilter(e.target.value)}
              placeholder="assignee (exact match)"
              style={{ minWidth: 160 }}
            />
            <span className="meta" style={{ alignSelf: 'center' }}>{tasks.length} task{tasks.length === 1 ? '' : 's'}</span>
          </div>

          {tasksLoading && <div className="loading">Loading tasks…</div>}
          {!tasksLoading && tasks.length === 0 && <div className="empty">No tasks match these filters.</div>}
          {!tasksLoading && tasks.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Content</th>
                    <th>Project</th>
                    <th>Assignee</th>
                    <th>Source</th>
                    <th>Labels</th>
                    <th>Due</th>
                    <th>Clarified</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map(t => (
                    <tr key={t.id}>
                      <td style={{ maxWidth: 320, wordBreak: 'break-word' }}>{t.content}</td>
                      <td>{projectsById[t.project_id] || t.project_id || '—'}</td>
                      <td>{t.assignee_label || '—'}</td>
                      <td>{t.source_tag ? <code>{t.source_tag}</code> : '—'}</td>
                      <td>
                        {(t.labels && t.labels.length > 0)
                          ? <div className="meta-tags-row">{t.labels.map(l => <span key={l} className="meta-tag">{l}</span>)}</div>
                          : '—'}
                      </td>
                      <td>{t.due_date || '—'}</td>
                      <td>
                        {t.last_clarified_at
                          ? <span className="badge badge-success" title={fmtDate(t.last_clarified_at)}>Clarified</span>
                          : <span className="badge badge-neutral">Pending</span>}
                      </td>
                      <td>
                        <button className="btn btn-sm" disabled={reclarifyBusy === t.id}
                          onClick={() => reclarify(t.id)}>
                          {reclarifyBusy === t.id ? 'Queuing…' : 'Re-clarify'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* Clarify decisions log */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>Clarify decisions</h2>
        <div className="card">
          <div className="filter-bar">
            <select value={appliedFilter} onChange={e => setAppliedFilter(e.target.value as '' | 'true' | 'false')}>
              <option value="">All</option>
              <option value="true">Applied</option>
              <option value="false">Not applied</option>
            </select>
            <span className="meta" style={{ alignSelf: 'center' }}>{clarifyLog.length} decision{clarifyLog.length === 1 ? '' : 's'} · last 50</span>
          </div>

          {clarifyLoading && <div className="loading">Loading clarify log…</div>}
          {!clarifyLoading && clarifyLog.length === 0 && <div className="empty">No clarify decisions match this filter.</div>}
          {!clarifyLoading && clarifyLog.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Classification</th>
                    <th>Confidence</th>
                    <th>Assignee</th>
                    <th>Source</th>
                    <th>Applied</th>
                    <th>Model</th>
                    <th>Created</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {clarifyLog.map(l => {
                    const expanded = expandedReasons.has(l.id);
                    const reason = l.reason || '';
                    return (
                      <tr key={l.id}>
                        <td style={{ maxWidth: 260, wordBreak: 'break-word' }}>{l.task_content || l.todoist_task_id}</td>
                        <td>{l.classification ? <span className="badge badge-type">{l.classification}</span> : '—'}</td>
                        <td>{fmtConfidence(l.confidence)}</td>
                        <td>{l.assignee || '—'}</td>
                        <td>{l.source_tag ? <code>{l.source_tag}</code> : '—'}</td>
                        <td>{l.applied ? <span className="badge badge-success">applied</span> : <span className="badge badge-neutral">not applied</span>}</td>
                        <td>{l.llm_model || '—'}</td>
                        <td>{fmtDate(l.created_at)}</td>
                        <td style={{ maxWidth: 320, cursor: reason.length > 80 ? 'pointer' : 'default' }}
                          onClick={() => reason.length > 80 && toggleReason(l.id)}>
                          {reason
                            ? (expanded
                              ? <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{reason}</span>
                              : <span title={reason.length > 80 ? 'Click to expand' : undefined}>{truncate(reason, 80)}</span>)
                            : '—'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* Sync health */}
      <h2 style={{ fontSize: 16, fontWeight: 600, marginTop: 28 }}>Sync health</h2>

      {/* Summary cards */}
      <section style={{ marginTop: 12, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Open tasks</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.open ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Completed (7d)</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.completed_7d ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Pending clarify</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.pending_clarify ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Outbox pending</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>
            {pendingCount}
            {pendingCount > 0 && (
              <span style={{ fontSize: 12, marginLeft: 8, opacity: 0.7 }}>
                oldest {fmtAge(data?.outbox?.oldest_pending_age_seconds ?? null)}
              </span>
            )}
          </div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Outbox failed</div>
          <div style={{ fontSize: 24, fontWeight: 600, color: failedCount > 0 ? 'var(--error, #c0392b)' : undefined }}>
            {failedCount}
          </div>
        </div>
      </section>

      {/* Sync watermarks */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>Sync state</h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Last full sync</th>
                <th>Last incremental</th>
                <th>Managed projects</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{data?.sync?.last_full_sync_at ? new Date(data.sync.last_full_sync_at).toLocaleString() : '—'}</td>
                <td>{data?.sync?.last_incremental_at ? new Date(data.sync.last_incremental_at).toLocaleString() : '—'}</td>
                <td style={{ wordBreak: 'break-word' }}>
                  {data?.managed_projects
                    ? Object.entries(data.managed_projects).map(([k, v]) => `${k}: ${v}`).join(' · ')
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      {/* Failed outbox commands — lost writes */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>
          Failed outbox commands {failedCount > 0 && <span className="badge badge-error">lost writes</span>}
        </h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Command</th>
                <th>Attempts</th>
                <th>Last attempt</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {(data?.outbox?.failed_recent?.length ?? 0) === 0 && (
                <tr><td colSpan={5} className="empty">No failed commands ✨</td></tr>
              )}
              {data?.outbox?.failed_recent?.map((r: any) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td><strong>{r.command_type}</strong></td>
                  <td>{r.attempt_count}</td>
                  <td>{r.last_attempt_at ? new Date(r.last_attempt_at).toLocaleString() : '—'}</td>
                  <td>{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
