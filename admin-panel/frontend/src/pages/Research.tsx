import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import { toast } from '../components/Toast';
import {
  numbersPayload,
  splitList,
  toAreaRow,
  toAreasPayload,
  toTopicRow,
  toTopicsPayload,
  type AreaRow,
  type TopicRow,
} from '../lib/researchConfig';

// The research lane's DB-owned config (the research-tagged agent's work:
// tracked topics, feed health, research and library limits). Every card is
// its own settings row with its own Save; the server validates and answers
// 400 with a reason, which lands in the banner. Defaults are the code's, so
// a deployment that never saves anything runs as before.

type Numbers = Record<string, string>;

const strs = (obj: Record<string, unknown>, keys: string[]): Numbers =>
  Object.fromEntries(keys.map(k => [k, obj?.[k] === undefined || obj?.[k] === null ? '' : String(obj[k])]));

const FEED_KEYS = ['failing_after', 'recovered_after', 'stale_after_days', 'unused_after_days', 'stale_review_hour'];
const FEED_HELP: Record<string, string> = {
  failing_after: 'Failed fetches in a row before a feed is a problem on the hub (hourly runs, so 3 is three hours).',
  recovered_after: 'Good fetches in a row before that problem resolves.',
  stale_after_days: 'Days without a stored entry before a feed is reported stale. A feed\'s own value on Channels wins.',
  unused_after_days: 'History a feed needs before the monthly briefing may call it unused, and the window "used" is measured over.',
  stale_review_hour: 'The UTC hour whose hourly run reconciles stale feeds (0-23).',
};

const RESEARCH_KEYS = ['wait_seconds', 'page_chars', 'report_chars', 'knowledge_hits', 'note_hits'];
const RESEARCH_HELP: Record<string, string> = {
  wait_seconds: 'How long research_topic waits for the flow before saying "still researching" (5-300).',
  page_chars: 'Characters of one read page that go into the synthesis prompt.',
  report_chars: 'A report posted as a task comment or chat reply is cut here.',
  knowledge_hits: 'Knowledge-store documents gathered per run (0 = skip).',
  note_hits: 'Vault notes gathered ahead of them, when the vault is configured.',
};
const DEPTH_KEYS = ['pages', 'web_results', 'papers'];

const LIBRARY_KEYS = [
  'read_chars', 'passages', 'passage_chars', 'pdf_default_pages',
  'research_book_hits', 'research_passage_min_similarity', 'research_passage_chars', 'research_pdf_scan_pages',
];
const LIBRARY_HELP: Record<string, string> = {
  read_chars: 'What library_read returns when the model names no size (500-40000).',
  passages: 'Best-matching windows a query returns.',
  passage_chars: 'About this many characters per window.',
  pdf_default_pages: 'Pages a PDF read returns with no pages, section or query (at most 30).',
  research_book_hits: 'Books ResearchFlow considers (0 = skip the library).',
  research_passage_min_similarity: 'How close the best book must be (0-1) before a passage is read from it.',
  research_passage_chars: 'Characters of passage handed to the synthesis.',
  research_pdf_scan_pages: 'PDF pages research scans for a passage.',
};

function NumberField({ k, value, help, onChange }: { k: string; value: string; help?: string; onChange: (v: string) => void }) {
  return (
    <div className="form-group" style={{ marginBottom: 10 }}>
      <label style={{ fontFamily: 'monospace', fontSize: 12 }}>{k}</label>
      <input value={value} onChange={e => onChange(e.target.value)} className="mono" style={{ maxWidth: 160 }} />
      {help && <p className="meta" style={{ margin: '4px 0 0' }}>{help}</p>}
    </div>
  );
}

export default function Research() {
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string>('');

  const [topics, setTopics] = useState<TopicRow[]>([]);
  const [rounds, setRounds] = useState<Record<string, any>>({});
  const [priorities, setPriorities] = useState<string[]>(['high', 'medium', 'low']);
  const [attention, setAttention] = useState<Numbers>({});
  const [digestItems, setDigestItems] = useState('');
  const [areas, setAreas] = useState<AreaRow[]>([]);
  const [cadences, setCadences] = useState<string[]>(['daily', 'weekly', 'vault']);
  const [briefCfg, setBriefCfg] = useState<Numbers>({});
  const [feedCfg, setFeedCfg] = useState<Numbers>({});
  const [defaultIngest, setDefaultIngest] = useState('full');
  const [research, setResearch] = useState<Numbers>({});
  const [depths, setDepths] = useState<Record<string, Numbers>>({ quick: {}, thorough: {} });
  const [academic, setAcademic] = useState('');
  const [library, setLibrary] = useState<Numbers>({});
  const [stopwords, setStopwords] = useState('');

  function applyTopics(r: any) {
    setTopics((r.topics || []).map(toTopicRow));
    setRounds(Object.fromEntries((r.topics || []).map((t: any) => [t.name, t])));
    setPriorities(r.priorities || ['high', 'medium', 'low']);
    setAreas((r.areas || []).map(toAreaRow));
    setCadences(r.cadences || ['daily', 'weekly', 'vault']);
    if (r.config) applyTopicsConfig(r.config);
  }
  function applyTopicsConfig(c: any) {
    setAttention(strs(c.attention || {}, ['high', 'medium', 'low']));
    setDigestItems(String(c.digest_items ?? ''));
    setBriefCfg(strs(c, ['brief_items', 'weekly_day']));
  }
  function applyFeeds(c: any) {
    setFeedCfg(strs(c, FEED_KEYS));
    setDefaultIngest(c.default_ingest || 'full');
  }
  function applyResearch(c: any) {
    setResearch(strs(c, RESEARCH_KEYS));
    setDepths({ quick: strs(c.depths?.quick || {}, DEPTH_KEYS), thorough: strs(c.depths?.thorough || {}, DEPTH_KEYS) });
    setAcademic((c.academic_terms || []).join(', '));
  }
  function applyLibrary(c: any) {
    setLibrary(strs(c, LIBRARY_KEYS));
    setStopwords((c.stopwords || []).join(', '));
  }

  async function load() {
    setError(null); setLoading(true);
    try {
      const [t, f, r, l] = await Promise.all([
        api.getResearchTopics(), api.getFeedsConfig(), api.getResearchConfig(), api.getLibraryConfig(),
      ]);
      applyTopics(t); applyFeeds(f); applyResearch(r); applyLibrary(l);
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function run(card: string, fn: () => Promise<void>, done: string) {
    setError(null); setSaving(card);
    try { await fn(); toast.ok(done); }
    catch (e: any) { setError(e); }
    finally { setSaving(''); }
  }

  const saveTopics = () => run('topics', async () => {
    applyTopics(await api.saveResearchTopics(toTopicsPayload(topics)));
  }, 'Tracked topics saved — the next scan and feed run use them.');

  // Areas ride the same registry row, so they save with the topics as shown.
  const saveAreas = () => run('areas', async () => {
    applyTopics(await api.saveResearchTopics({ ...toTopicsPayload(topics), areas: toAreasPayload(areas) }));
  }, 'Areas saved — the next morning brief uses them.');

  const saveTopicsConfig = () => run('topics-config', async () => {
    const body: any = { attention: numbersPayload(attention, ['high', 'medium', 'low']) };
    const d = digestItems.trim(); if (d) body.digest_items = Number.isFinite(Number(d)) ? Number(d) : d;
    Object.assign(body, numbersPayload(briefCfg, ['brief_items', 'weekly_day']));
    applyTopicsConfig(await api.saveResearchTopicsConfig(body));
  }, 'Topic thresholds saved — applies within ~30s.');

  const saveFeeds = () => run('feeds', async () => {
    applyFeeds(await api.saveFeedsConfig({ ...numbersPayload(feedCfg, FEED_KEYS), default_ingest: defaultIngest }));
  }, 'Feed health saved — the next hourly run uses it.');

  const saveResearch = () => run('research', async () => {
    applyResearch(await api.saveResearchConfig({
      ...numbersPayload(research, RESEARCH_KEYS),
      depths: { quick: numbersPayload(depths.quick, DEPTH_KEYS), thorough: numbersPayload(depths.thorough, DEPTH_KEYS) },
      academic_terms: splitList(academic),
    }));
  }, 'Research limits saved — applies within ~30s.');

  const saveLibrary = () => run('library', async () => {
    applyLibrary(await api.saveLibraryConfig({ ...numbersPayload(library, LIBRARY_KEYS), stopwords: splitList(stopwords) }));
  }, 'Library limits saved — applies within ~30s.');

  const setTopic = (i: number, patch: Partial<TopicRow>) =>
    setTopics(rows => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div>
      <h1 className="page-title">Research</h1>
      <p className="page-subtitle">
        The research agent's config: what it tracks, when a feed is a problem, how far a research run
        goes and how much of a book it reads. Every default is the code's; nothing here is baked in.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {loading && <div className="loading">Loading…</div>}

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Tracked topics</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          The registry the intel scans search and the feed gate matches on (<code>settings.intelligence_topics</code>).
          A scan searches each topic once, by its name; an article belongs to a topic when it names one of its match terms.
          Each topic keeps one open round of news on the hub; the round becomes a Todoist task once it holds
          the topic's threshold of items (its own number, else its priority's below). Saving replaces the list:
          a removed topic's round is closed, a new one's is opened.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Topic</th><th>Match terms (comma-separated)</th>
                <th style={{ width: 110 }}>Priority</th><th style={{ width: 90 }}>Threshold</th>
                <th style={{ width: 220 }}>Round</th><th style={{ width: 50 }} />
              </tr>
            </thead>
            <tbody>
              {topics.map((t, i) => {
                const live = rounds[t.name]?.round;
                return (
                  <tr key={i}>
                    <td><input value={t.name} onChange={e => setTopic(i, { name: e.target.value })} style={{ width: '100%' }} /></td>
                    <td><input value={t.queries} placeholder="defaults to the name" onChange={e => setTopic(i, { queries: e.target.value })} style={{ width: '100%' }} /></td>
                    <td>
                      <select value={t.priority} onChange={e => setTopic(i, { priority: e.target.value })}>
                        {priorities.map(p => <option key={p} value={p}>{p}</option>)}
                      </select>
                    </td>
                    <td><input value={t.threshold} placeholder={attention[t.priority] || ''} onChange={e => setTopic(i, { threshold: e.target.value })} className="mono" style={{ width: 70 }} /></td>
                    <td className="meta">
                      {live
                        ? <>{live.items} item{live.items === 1 ? '' : 's'} · {live.status}{live.task_id ? ` · task ${live.task_id}` : live.attention ? ' · task pending' : ''}</>
                        : rounds[t.name] ? 'no live round' : 'new'}
                    </td>
                    <td><button className="btn btn-sm" onClick={() => setTopics(rows => rows.filter((_, j) => j !== i))}>✕</button></td>
                  </tr>
                );
              })}
              {topics.length === 0 && <tr><td colSpan={6} className="empty">Nothing tracked. The scans run on their own topics; no round opens.</td></tr>}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => setTopics(r => [...r, { name: '', queries: '', priority: 'medium', threshold: '' }])}>+ Add topic</button>
          <button className="btn btn-primary" disabled={saving === 'topics'} onClick={saveTopics}>{saving === 'topics' ? 'Saving…' : 'Save topics'}</button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Areas</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          How news reaches you. Each area groups topics; every morning the brief shows what <em>changed</em> in its
          daily areas — related articles folded into one story, judged against your "why", at most <code>cap</code> a
          day. Weekly areas come on <code>weekly_day</code>; vault areas go to that week's journal note and never
          interrupt. A topic in an area never becomes a Todoist task for how much was written about it.
          With no areas, the brief lists topics and scan finds as before.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 160 }}>Area</th><th>Why you care</th>
                <th style={{ width: 110 }}>Cadence</th><th style={{ width: 70 }}>Cap</th>
                <th>Topics (comma-separated)</th><th style={{ width: 50 }} />
              </tr>
            </thead>
            <tbody>
              {areas.map((a, i) => {
                const set = (patch: Partial<AreaRow>) => setAreas(rows => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
                return (
                  <tr key={i}>
                    <td><input value={a.name} onChange={e => set({ name: e.target.value })} style={{ width: '100%' }} /></td>
                    <td><input value={a.why} placeholder="one sentence, in your words" onChange={e => set({ why: e.target.value })} style={{ width: '100%' }} /></td>
                    <td>
                      <select value={a.cadence} onChange={e => set({ cadence: e.target.value })}>
                        {cadences.map(c => <option key={c} value={c}>{c}</option>)}
                      </select>
                    </td>
                    <td><input value={a.cap} placeholder="3" onChange={e => set({ cap: e.target.value })} className="mono" style={{ width: 50 }} /></td>
                    <td><input value={a.topics} onChange={e => set({ topics: e.target.value })} style={{ width: '100%' }} /></td>
                    <td><button className="btn btn-sm" onClick={() => setAreas(rows => rows.filter((_, j) => j !== i))}>✕</button></td>
                  </tr>
                );
              })}
              {areas.length === 0 && <tr><td colSpan={6} className="empty">No areas. The brief lists topics and scan finds the old way.</td></tr>}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => setAreas(r => [...r, { name: '', why: '', cadence: 'daily', cap: '', topics: '' }])}>+ Add area</button>
          <button className="btn btn-primary" disabled={saving === 'areas'} onClick={saveAreas}>{saving === 'areas' ? 'Saving…' : 'Save areas'}</button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Topic thresholds</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          Items a round must hold, per priority, before the topic interrupts you with a task
          (<code>research_topics_config</code>). One new article is news, not a chore; a high-priority topic asks sooner.
        </p>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          {['high', 'medium', 'low'].map(p => (
            <NumberField key={p} k={p} value={attention[p] || ''} onChange={v => setAttention(a => ({ ...a, [p]: v }))} />
          ))}
          <NumberField k="digest_items" value={digestItems} help="Items a round's task and briefing digest list, newest first." onChange={setDigestItems} />
          <NumberField k="brief_items" value={briefCfg.brief_items || ''} help="Area stories in one morning brief, across all areas, spent in the areas' order." onChange={v => setBriefCfg(c => ({ ...c, brief_items: v }))} />
          <NumberField k="weekly_day" value={briefCfg.weekly_day || ''} help="The day weekly and vault areas get their digest: 0 = Monday … 6 = Sunday, on your clock." onChange={v => setBriefCfg(c => ({ ...c, weekly_day: v }))} />
        </div>
        <button className="btn btn-primary" disabled={saving === 'topics-config'} onClick={saveTopicsConfig}>{saving === 'topics-config' ? 'Saving…' : 'Save thresholds'}</button>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Feed health</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          When RssIngestFlow reports a feed to the hub, and when the monthly briefing calls one unused
          (<code>feeds_config</code>). Per-feed overrides live on the Channels page.
        </p>
        {FEED_KEYS.map(k => <NumberField key={k} k={k} value={feedCfg[k] || ''} help={FEED_HELP[k]} onChange={v => setFeedCfg(c => ({ ...c, [k]: v }))} />)}
        <div className="form-group" style={{ marginBottom: 10 }}>
          <label style={{ fontFamily: 'monospace', fontSize: 12 }}>default_ingest</label>
          <select value={defaultIngest} onChange={e => setDefaultIngest(e.target.value)} style={{ maxWidth: 160 }}>
            {['full', 'abstract', 'gate'].map(m => <option key={m} value={m}>{m}</option>)}
          </select>
          <p className="meta" style={{ margin: '4px 0 0' }}>The ingest mode for a feed that sets none of its own (and for a feed subscribed from chat).</p>
        </div>
        <button className="btn btn-primary" disabled={saving === 'feeds'} onClick={saveFeeds}>{saving === 'feeds' ? 'Saving…' : 'Save feed health'}</button>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Research limits</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          How far a research run goes (<code>research_config</code>): what it gathers per depth, how much of each
          page the model sees, and how long the chat tool waits. The tool schema caps (60,000 characters a read)
          stay in code.
        </p>
        {RESEARCH_KEYS.map(k => <NumberField key={k} k={k} value={research[k] || ''} help={RESEARCH_HELP[k]} onChange={v => setResearch(c => ({ ...c, [k]: v }))} />)}
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
          {(['quick', 'thorough'] as const).map(d => (
            <div key={d}>
              <h3 style={{ fontSize: 13, margin: '8px 0' }}>depth: {d}</h3>
              {DEPTH_KEYS.map(k => (
                <NumberField key={k} k={k} value={depths[d]?.[k] || ''} onChange={v => setDepths(x => ({ ...x, [d]: { ...x[d], [k]: v } }))} />
              ))}
            </div>
          ))}
        </div>
        <div className="form-group" style={{ marginBottom: 10 }}>
          <label style={{ fontFamily: 'monospace', fontSize: 12 }}>academic_terms</label>
          <input value={academic} onChange={e => setAcademic(e.target.value)} className="mono" style={{ width: '100%' }} />
          <p className="meta" style={{ margin: '4px 0 0' }}>
            Words (regular expressions, comma-separated) that make a question worth a paper search on arXiv and Semantic Scholar.
          </p>
        </div>
        <button className="btn btn-primary" disabled={saving === 'research'} onClick={saveResearch}>{saving === 'research' ? 'Saving…' : 'Save research limits'}</button>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Library limits</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          How much of a Calibre book a read returns and how research uses the library (<code>library_config</code>).
          The connection itself, and the file-size and catalogue caps, are on Integrations → Calibre (library).
        </p>
        {LIBRARY_KEYS.map(k => <NumberField key={k} k={k} value={library[k] || ''} help={LIBRARY_HELP[k]} onChange={v => setLibrary(c => ({ ...c, [k]: v }))} />)}
        <div className="form-group" style={{ marginBottom: 10 }}>
          <label style={{ fontFamily: 'monospace', fontSize: 12 }}>stopwords</label>
          <textarea value={stopwords} onChange={e => setStopwords(e.target.value)} rows={3} style={{ width: '100%' }} />
          <p className="meta" style={{ margin: '4px 0 0' }}>Words passage search ignores when matching a query (comma-separated).</p>
        </div>
        <button className="btn btn-primary" disabled={saving === 'library'} onClick={saveLibrary}>{saving === 'library' ? 'Saving…' : 'Save library limits'}</button>
      </div>
    </div>
  );
}
