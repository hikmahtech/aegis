/**
 * The books' entities and chart of accounts.
 *
 * The money lane used to have one operator's accounting compiled in — two sets
 * of books, the account segment that told them apart, and every category →
 * account mapping. AEGIS is forked and configured for someone else's life, so
 * all of that is now configuration, and this is where it is answered.
 *
 * Two things it deliberately is not:
 *
 * * **Not the journal.** Nothing here creates an account. hledger's own
 *   `account` declarations are the chart of accounts; this says which of them
 *   a category posts to, and `check --strict` still refuses anything the
 *   journal has not declared.
 * * **Not a second opinion.** The server sends the chart through the same
 *   lenient `merge` every post reads, and refuses anything that would not work
 *   with a 400 naming the field — shown here verbatim. A form that showed a
 *   different answer from the one the books file by would be worse than none.
 */

import { useEffect, useState } from 'react';
import { BOX, Field } from '../lib/formFields';
import { moneyApi, type BooksChart, type ChartEntity } from '../lib/moneyApi';

const BLANK_ENTITY: ChartEntity = {
  label: '',
  segment: '',
  unknown: { in: 'income:unknown', out: 'expenses:unknown' },
  categories: {},
};

/** An entity id as the server will accept it: lowercase, no spaces. */
function asId(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').slice(0, 32);
}

export default function ChartPanel({ onSaved }: { onSaved?: () => void }) {
  const [chart, setChart] = useState<BooksChart | null>(null);
  const [stored, setStored] = useState(false);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [newId, setNewId] = useState('');

  useEffect(() => {
    let live = true;
    void moneyApi.chart().then(
      s => { if (live) { setChart(s.chart); setStored(s.stored); } },
      e => { if (live) setError((e as Error)?.message || 'Could not read the chart'); },
    );
    return () => { live = false; };
  }, []);

  if (!chart) {
    return (
      <section className="section">
        <h2 className="section-title">Its entities</h2>
        {error ? <div className="error">{error}</div> : <div className="loading">Reading the chart…</div>}
      </section>
    );
  }

  const ids = Object.keys(chart.entities);

  const edit = (id: string, patch: Partial<ChartEntity>) => {
    setChart(c => (c ? { ...c, entities: { ...c.entities, [id]: { ...c.entities[id], ...patch } } } : c));
    setSaved(false);
  };

  const setCategories = (id: string, rows: [string, string][]) =>
    edit(id, { categories: Object.fromEntries(rows.filter(([name]) => name.trim())) });

  function addEntity() {
    const id = asId(newId);
    if (!id || chart!.entities[id]) return;
    setChart(c => (c ? { ...c, entities: { ...c.entities, [id]: { ...BLANK_ENTITY, segment: id } } } : c));
    setNewId('');
    setSaved(false);
  }

  function removeEntity(id: string) {
    setChart(c => {
      if (!c) return c;
      const rest = Object.fromEntries(Object.entries(c.entities).filter(([k]) => k !== id));
      return { ...c, entities: rest };
    });
    setSaved(false);
  }

  async function save() {
    if (!chart) return;
    setSaving(true);
    setError('');
    try {
      const next = await moneyApi.saveChart(chart);
      setChart(next.chart);
      setStored(next.stored);
      setSaved(true);
      onSaved?.();
    } catch (e) {
      // Verbatim: the server's sentence names the field, and a friendlier
      // rewrite here would lose which one.
      setError((e as Error)?.message || 'The chart was not saved');
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="section">
      <div className="section-header-row">
        <h2 className="section-title" style={{ marginBottom: 0 }}>Its entities</h2>
        <button className="btn btn-sm" onClick={() => setOpen(o => !o)}>
          {open ? 'Hide' : 'Change the chart'}
        </button>
      </div>

      <p className="meta" style={{ marginBottom: 12 }}>
        Which sets of books exist, which account-name segment marks each one, and which
        category posts to which account. Money nothing explains goes to an entity&rsquo;s{' '}
        <em>unknown</em> account, which is the review queue. Nothing here creates an account:
        the journal&rsquo;s own <code>account</code> declarations still decide what exists.
      </p>

      {!stored && (
        <p className="meta" style={{ marginBottom: 12 }}>
          Still on the built-in default — one set of books called{' '}
          <code>{chart.default_entity}</code> with generic categories. Saving here makes it
          yours.
        </p>
      )}

      {error && <div className="error" style={{ marginBottom: 12 }}>{error}</div>}

      {!open ? (
        <div className="card">
          {ids.map(id => (
            <div className="meta-row" key={id}>
              <span>
                {chart.entities[id].label || id}
                {id === chart.default_entity && <span className="meta"> · default</span>}
              </span>
              <span className="mono">
                {chart.entities[id].segment
                  ? `:${chart.entities[id].segment}:`
                  : 'everything else'}
                {' · '}
                {Object.keys(chart.entities[id].categories).length} categories
              </span>
            </div>
          ))}
        </div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <Field
              label="Default set of books"
              hint="Where a transaction goes when nothing names an entity. It owns every expense and income account no other entity's segment claims, so it has no segment of its own."
            >
              <select
                style={BOX}
                value={chart.default_entity}
                onChange={e => { setChart(c => (c ? { ...c, default_entity: e.target.value } : c)); setSaved(false); }}
              >
                {ids.map(id => <option key={id} value={id}>{chart.entities[id].label || id}</option>)}
              </select>
            </Field>
            <Field
              label="Categories that mean money coming in"
              hint="Comma separated. A receipt tagged with one of these is treated as a credit even when it never said which way the money went."
            >
              <input
                type="text" style={BOX} placeholder="salary, interest, refund"
                value={chart.income_categories.join(', ')}
                onChange={e => {
                  const list = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
                  setChart(c => (c ? { ...c, income_categories: list } : c));
                  setSaved(false);
                }}
              />
            </Field>
          </div>

          <div className="grid">
            {ids.map(id => {
              const ent = chart.entities[id];
              const rows = Object.entries(ent.categories);
              const isDefault = id === chart.default_entity;
              return (
                <div className="card" key={id}>
                  <div className="section-header-row">
                    <h3 style={{ marginBottom: 0 }}>
                      <code>{id}</code>{isDefault && <span className="meta"> · default</span>}
                    </h3>
                    {!isDefault && (
                      <button className="btn btn-sm" onClick={() => removeEntity(id)}>Remove</button>
                    )}
                  </div>

                  <Field label="Name" hint="What this set of books is called in the weekly brief.">
                    <input
                      type="text" style={BOX} placeholder={id}
                      value={ent.label} onChange={e => edit(id, { label: e.target.value })}
                    />
                  </Field>
                  <Field
                    label="Account segment"
                    hint={isDefault
                      ? 'The default set of books has none: it owns whatever no other segment claims.'
                      : 'The middle of its account names — a segment of "acme" makes expenses:acme:rent and income:acme:fees this entity’s.'}
                  >
                    <input
                      type="text" style={BOX} placeholder={isDefault ? '' : 'acme'}
                      disabled={isDefault}
                      value={ent.segment}
                      onChange={e => edit(id, { segment: asId(e.target.value) })}
                    />
                  </Field>
                  <Field label="Unknown account, money out" hint="Where an unexplained payment lands. It has to be in the expenses tree.">
                    <input
                      type="text" style={BOX} placeholder="expenses:unknown"
                      value={ent.unknown.out}
                      onChange={e => edit(id, { unknown: { ...ent.unknown, out: e.target.value } })}
                    />
                  </Field>
                  <Field label="Unknown account, money in" hint="Where an unexplained credit lands. It has to be in the income tree.">
                    <input
                      type="text" style={BOX} placeholder="income:unknown"
                      value={ent.unknown.in}
                      onChange={e => edit(id, { unknown: { ...ent.unknown, in: e.target.value } })}
                    />
                  </Field>

                  <Field
                    label="Categories"
                    hint="What a category the extractor recognises posts to. A category with no row here goes to the unknown account above."
                  >
                    <>
                      {rows.map(([name, account], i) => (
                        <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6, maxWidth: 420 }}>
                          <input
                            type="text" style={{ flex: 1 }} placeholder="groceries" value={name}
                            onChange={e => setCategories(id, rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
                          />
                          <input
                            type="text" style={{ flex: 2 }} placeholder="expenses:groceries" value={account}
                            onChange={e => setCategories(id, rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
                          />
                          <button className="btn btn-sm" onClick={() => setCategories(id, rows.filter((_, j) => j !== i))}>
                            ✕
                          </button>
                        </div>
                      ))}
                      {!rows.length && (
                        <div className="empty">
                          No categories. Everything goes to the unknown accounts above.
                        </div>
                      )}
                      <button
                        className="btn btn-sm" style={{ marginTop: 4 }}
                        onClick={() => setCategories(id, [...rows, ['', '']])}
                      >
                        + Add a category
                      </button>
                    </>
                  </Field>
                </div>
              );
            })}

            <div className="card">
              <h3>Another set of books</h3>
              <Field
                label="Its id"
                hint="Lowercase, no spaces. It names the journal directory its transactions are filed in, so it is worth getting right first time."
              >
                <input
                  type="text" style={BOX} placeholder="acme"
                  value={newId} onChange={e => setNewId(e.target.value)}
                />
              </Field>
              <button className="btn btn-sm" disabled={!asId(newId)} onClick={addEntity}>
                + Add it
              </button>
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <button className="btn btn-primary" disabled={saving} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save the chart'}
            </button>
            {saved && (
              <span className="meta" style={{ marginLeft: 12 }}>
                Saved. The money lane reads this on the next transaction — no restart needed.
              </span>
            )}
          </div>
        </>
      )}
    </section>
  );
}
