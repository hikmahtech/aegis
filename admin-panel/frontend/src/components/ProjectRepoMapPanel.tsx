import { useState } from 'react';
import { api } from '../api/client';
import { useConfigRow } from '../lib/useConfigRow';
import ErrorBanner from './ErrorBanner';

// Todoist project name → GitHub repo (`project_repo_map`, #345/#558): the
// coding lane's first guess at which checkout a `@code` task is about. It
// ships empty so a fork carries nobody's projects, which means a new
// deployment has to fill it in — here, not with psql.

type Row = [string, string];

export default function ProjectRepoMapPanel({ projectNames = [] }: { projectNames?: string[] }) {
  const [rows, setRows] = useState<Row[]>([]);
  const { error, setError, saving, save } = useConfigRow(async () => {
    setRows(Object.entries((await api.getProjectRepoMap()).project_repo_map || {}));
  });

  const onSave = () => save(async () => {
    const map: Record<string, string> = {};
    // A half-filled row is sent as-is so the server's 400 names it.
    for (const [name, repo] of rows) if (name.trim() || repo.trim()) map[name.trim()] = repo.trim();
    const r = await api.saveProjectRepoMap(map);
    setRows(Object.entries(r.project_repo_map || {}));
  }, 'Project → repo map saved. The next coding task reads it.');

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>Project → repository</h3>
      <p className="page-subtitle">
        Which GitHub repository a Todoist project's <code>@code</code> tasks are about. The coding
        lane tries this first; a project not listed falls through to guessing from the task title.
        Project names match case-insensitively. Ships empty.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div className="table-scroll">
        <table style={{ width: '100%', fontSize: 13 }}>
          <thead><tr>
            <th style={{ textAlign: 'left' }}>Todoist project</th>
            <th style={{ textAlign: 'left' }}>Repository (owner/name)</th><th />
          </tr></thead>
          <tbody>
            {rows.map(([name, repo], i) => (
              <tr key={i}>
                <td><input style={{ width: '100%' }} value={name} list="todoist-project-names"
                  placeholder="Home Infra"
                  onChange={e => setRows(rs => rs.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} /></td>
                <td><input style={{ width: '100%' }} value={repo} placeholder="acme/infra"
                  onChange={e => setRows(rs => rs.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} /></td>
                <td><button className="btn btn-sm" onClick={() => setRows(rs => rs.filter((_, j) => j !== i))}>✕</button></td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={3} style={{ color: 'var(--text-muted)' }}>
                No projects mapped. Every coding task guesses its repo from the title.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <datalist id="todoist-project-names">
        {projectNames.map(n => <option key={n} value={n} />)}
      </datalist>
      <button className="btn" style={{ marginTop: 8 }} onClick={() => setRows(rs => [...rs, ['', '']])}>
        + Add project
      </button>
      <button className="btn btn-primary" style={{ marginTop: 8, marginLeft: 8 }} disabled={saving} onClick={onSave}>
        {saving ? 'Saving…' : 'Save project map'}
      </button>
    </div>
  );
}
