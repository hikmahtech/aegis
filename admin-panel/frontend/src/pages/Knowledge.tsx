import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

function extractFolderId(s: string): string {
  const m = s.match(/\/folders\/([^/?#]+)/);
  return (m ? m[1] : s).trim();
}

export default function Knowledge() {
  const [askQ, setAskQ] = useState('');
  const [askAnswer, setAskAnswer] = useState<any>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);

  // One-off seeding
  const [url, setUrl] = useState('');
  const [folder, setFolder] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [seedMsg, setSeedMsg] = useState('');
  const [seeding, setSeeding] = useState(false);

  // Watched Drive folders (the drive-sync activity config)
  const [driveAct, setDriveAct] = useState<any>(null);
  const [folders, setFolders] = useState<{ id: string; name?: string }[]>([]);
  const [driveAcct, setDriveAcct] = useState('');
  const [gAccounts, setGAccounts] = useState<any[]>([]);
  const [newFolder, setNewFolder] = useState('');
  const [newName, setNewName] = useState('');
  const [driveMsg, setDriveMsg] = useState('');
  const [savingDrive, setSavingDrive] = useState(false);

  // Browse
  const [items, setItems] = useState<any[]>([]);
  const [browseQ, setBrowseQ] = useState('');

  async function refreshItems() {
    try {
      setItems(browseQ.trim()
        ? await api.knowledgeSearch(browseQ, 25)
        : await api.listKnowledgeContent(50));
    } catch (err: any) { setError(err); }
  }

  async function loadDrive() {
    try {
      const acts = await api.listActivities();
      const da = acts.find((a: any) => a.workflow_type === 'DriveSyncFlow');
      if (da) {
        setDriveAct(da);
        const cfg = da.config || {};
        const fl = (cfg.folders && cfg.folders.length)
          ? cfg.folders
          : (cfg.folder_id ? [{ id: cfg.folder_id }] : []);
        setFolders(fl);
        setDriveAcct(cfg.account || '');
      }
      setGAccounts(await api.listGoogleAccounts());
    } catch (err: any) { setError(err); }
  }

  useEffect(() => { refreshItems(); loadDrive(); /* eslint-disable-next-line */ }, []);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!askQ.trim()) return;
    setError(null); setLoading(true); setAskAnswer(null);
    try { setAskAnswer(await api.knowledgeAsk(askQ)); }
    catch (err: any) { setError(err); } finally { setLoading(false); }
  }

  async function seed(kind: 'url' | 'folder' | 'file') {
    setError(null); setSeeding(true); setSeedMsg('');
    try {
      if (kind === 'url' && url.trim()) {
        const r = await api.knowledgeIngestUrl(url.trim());
        setSeedMsg(`Ingested ${url} (${r.status}, ${r.chunks_total ?? 0} chunks)`);
        setUrl('');
      } else if (kind === 'folder' && folder.trim()) {
        const r = await api.knowledgeIngestFolder(folder.trim());
        setSeedMsg(`Folder: ${r.ingested} ingested, ${r.skipped} skipped, ${r.errors} errors`);
        setFolder('');
      } else if (kind === 'file' && file) {
        const r = await api.knowledgeUpload(file);
        setSeedMsg(`Uploaded ${file.name} (${r.status}, ${r.chunks_total ?? 0} chunks)`);
        setFile(null);
      }
      await refreshItems();
    } catch (err: any) { setError(err); } finally { setSeeding(false); }
  }

  function addFolder() {
    const id = extractFolderId(newFolder);
    if (!id || folders.some(f => f.id === id)) { setNewFolder(''); return; }
    setFolders([...folders, { id, name: newName.trim() || undefined }]);
    setNewFolder(''); setNewName('');
  }

  async function saveDrive() {
    if (!driveAct) return;
    setSavingDrive(true); setDriveMsg(''); setError(null);
    try {
      const config = { ...(driveAct.config || {}), account: driveAcct, folders, source_type: 'drive' };
      delete config.folder_id;  // migrate legacy single-folder to the folders list
      await api.updateActivity(driveAct.slug, { config });
      setDriveMsg('Saved — next sync (within ~5 min schedule reconcile + the 4h cadence) will pick it up.');
      await loadDrive();
    } catch (err: any) { setError(err); } finally { setSavingDrive(false); }
  }

  return (
    <div>
      <h1 className="page-title">Knowledge</h1>
      <p className="page-subtitle">Seed, browse, and ask over the local knowledge base (pgvector).</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginTop: 12 }}>
        <h3>Seed knowledge (one-off)</h3>
        <p className="page-subtitle">Feed in a web page, an uploaded file, or a server folder of docs.</p>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={url} onChange={e => setUrl(e.target.value)}
            placeholder="https://example.com/article" />
          <button className="btn" disabled={seeding || !url.trim()} onClick={() => seed('url')}>Add URL</button>
        </div>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input type="file" onChange={e => setFile(e.target.files?.[0] ?? null)} />
          <button className="btn" disabled={seeding || !file} onClick={() => seed('file')}>Upload</button>
        </div>
        <div className="cfg-row">
          <input value={folder} onChange={e => setFolder(e.target.value)}
            placeholder="/data/docs (server path)" />
          <button className="btn" disabled={seeding || !folder.trim()} onClick={() => seed('folder')}>Ingest folder</button>
        </div>
        {seedMsg && <p style={{ marginTop: 8, color: '#4caf50' }}>{seedMsg}</p>}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Watched Drive folders</h3>
        <p className="page-subtitle">
          AEGIS auto-ingests these folders every few hours (new/changed docs only, by Drive modifiedTime).
          {driveAct ? '' : ' (drive-sync flow not found)'}
        </p>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <span className="cfg-label">Account:</span>
          <select value={driveAcct} onChange={e => setDriveAcct(e.target.value)}>
            <option value="">— pick an account —</option>
            {gAccounts.map(a => (
              <option key={a.label} value={a.label} disabled={!a.has_drive}>
                {a.label}{a.has_drive ? '' : ' (needs drive re-auth)'}
              </option>
            ))}
          </select>
        </div>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={newFolder} onChange={e => setNewFolder(e.target.value)}
            placeholder="Drive folder URL or ID" onKeyDown={e => e.key === 'Enter' && addFolder()} />
          <input value={newName} onChange={e => setNewName(e.target.value)}
            placeholder="name (optional)" />
          <button className="btn" disabled={!newFolder.trim()} onClick={addFolder}>Add</button>
        </div>
        <div className="table-scroll">
        <table style={{ width: '100%' }}>
          <tbody>
            {folders.map((f, i) => (
              <tr key={f.id}>
                <td>{f.name || f.id}</td>
                <td style={{ fontSize: 11, color: '#888' }}><code>{f.id}</code></td>
                <td style={{ textAlign: 'right' }}>
                  <button className="btn" onClick={() => setFolders(folders.filter((_, j) => j !== i))}>Remove</button>
                </td>
              </tr>
            ))}
            {folders.length === 0 && <tr><td colSpan={3} style={{ color: '#888' }}>No folders watched yet.</td></tr>}
          </tbody>
        </table>
        </div>
        <button className="btn" style={{ marginTop: 8 }} disabled={savingDrive || !driveAct} onClick={saveDrive}>
          {savingDrive ? 'Saving…' : 'Save watched folders'}
        </button>
        {driveMsg && <p style={{ marginTop: 8, color: '#4caf50' }}>{driveMsg}</p>}
      </div>

      <form onSubmit={ask} className="card" style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <h3>Ask (RAG)</h3>
        <textarea value={askQ} onChange={e => setAskQ(e.target.value)} rows={3}
          placeholder="Ask anything the knowledge base might know…" />
        <button type="submit" className="btn" disabled={loading || !askQ.trim()}>
          {loading ? 'Thinking…' : 'Ask'}
        </button>
        {askAnswer && (
          <div style={{ marginTop: 8 }}>
            <p>{askAnswer.answer}</p>
            {askAnswer.sources?.length > 0 && (
              <ul>{askAnswer.sources.map((s: any, i: number) => (
                <li key={i}><code>{s.title || s.url || s.id || '—'}</code></li>
              ))}</ul>
            )}
          </div>
        )}
      </form>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={browseQ} onChange={e => setBrowseQ(e.target.value)}
            placeholder="Search the knowledge base…" onKeyDown={e => e.key === 'Enter' && refreshItems()} />
          <button className="btn" onClick={refreshItems}>{browseQ.trim() ? 'Search' : 'Refresh'}</button>
        </div>
        <div className="table-scroll">
        <table style={{ width: '100%' }}>
          <thead><tr><th style={{ textAlign: 'left' }}>Title</th><th>Type</th><th>Chunks</th><th>Ingested</th></tr></thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={it.content_id || i}>
                <td>{it.title || it.url || it.content_id}</td>
                <td style={{ textAlign: 'center' }}>{it.source_type}</td>
                <td style={{ textAlign: 'center' }}>{it.chunks_total ?? '—'}</td>
                <td style={{ textAlign: 'center' }}>{(it.ingested_at || '').slice(0, 10)}</td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={4}>Nothing ingested yet.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}
