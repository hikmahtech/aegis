/**
 * The trading desk's market and tax settings.
 *
 * The desk used to have one operator's market compiled in — an Indian clock,
 * an NSE calendar, a ".NS" ticker suffix, an April financial year and India's
 * capital gains rates. AEGIS is forked and configured for someone else's life,
 * so all of that is now configuration, and this is where it is answered.
 *
 * Deliberately small. It edits the settings that used to be Python constants
 * and nothing else: the trading bands, the order cap, the asset classes and
 * the benchmark price mappings stay on the Flows page, which edits the same
 * row as raw JSON. A second general config editor would just be a worse one.
 *
 * The server sends the values the desk itself reads (`desk_math.Rules`) and
 * refuses anything that would not work with a 400, so this form never has to
 * guess and never quietly stores a typo.
 */

import { useEffect, useState } from 'react';
import { moneyApi, type DeskRuleValues, type DeskRules } from '../lib/moneyApi';

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

/** One labelled field, with the sentence that says what it is for. */
function Field({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 14 }}>
      <span style={{ display: 'block', fontWeight: 600, marginBottom: 2 }}>{label}</span>
      <span className="meta" style={{ display: 'block', marginBottom: 4 }}>{hint}</span>
      {children}
    </label>
  );
}

const BOX: React.CSSProperties = { width: '100%', maxWidth: 320 };

export default function DeskRulesPanel({ onSaved }: { onSaved?: () => void }) {
  const [rules, setRules] = useState<DeskRules | null>(null);
  const [values, setValues] = useState<DeskRuleValues | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let live = true;
    void moneyApi.deskRules().then(
      r => { if (live) { setRules(r); setValues(r.values); } },
      e => { if (live) setError((e as Error)?.message || 'Could not read the desk settings'); },
    );
    return () => { live = false; };
  }, []);

  // The desk is off until a market is named, so an unconfigured desk opens the
  // panel rather than hiding the one thing that would turn it on.
  useEffect(() => { if (rules && !rules.configured) setOpen(true); }, [rules]);

  if (!values || !rules) {
    return (
      <section className="section">
        <h2 className="section-title">Its market</h2>
        {error ? <div className="error">{error}</div> : <div className="loading">Reading the settings…</div>}
      </section>
    );
  }

  const set = <K extends keyof DeskRuleValues>(key: K, value: DeskRuleValues[K]) => {
    setValues(v => (v ? { ...v, [key]: value } : v));
    setSaved(false);
  };

  const num = (key: keyof DeskRuleValues) => (raw: string) =>
    set(key, (raw === '' ? 0 : Number(raw)) as never);

  const taxRows = Object.entries(values.tax_rate);
  const setTax = (rows: [string, number][]) =>
    set('tax_rate', Object.fromEntries(rows.filter(([c]) => c.trim())) as never);

  async function save() {
    if (!values) return;
    setSaving(true);
    setError('');
    try {
      const next = await moneyApi.saveDeskRules(values);
      setRules(next);
      setValues(next.values);
      setSaved(true);
      onSaved?.();
    } catch (e) {
      setError((e as Error)?.message || 'The settings were not saved');
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="section">
      <div className="section-header-row">
        <h2 className="section-title" style={{ marginBottom: 0 }}>Its market</h2>
        <button className="btn btn-sm" onClick={() => setOpen(o => !o)}>
          {open ? 'Hide' : 'Change these settings'}
        </button>
      </div>

      <p className="meta" style={{ marginBottom: 12 }}>
        Which exchange the desk trades, on whose clock, in which currency, and under which
        tax law. AEGIS ships none of these, so a desk with no trading calendar does nothing
        at all. Everything else about the desk — the trading bands, the order cap, the asset
        classes — is on the Flows page, which edits the same row.
      </p>

      {!rules.configured && (
        <div
          style={{
            background: 'var(--warning-tint)',
            border: '1px solid var(--warning-text)',
            color: 'var(--warning-text)',
            padding: '10px 14px',
            margin: '0 0 1rem',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          <strong>No market is set, so the desk does nothing.</strong>
          <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>
            Name the instrument whose trading days the desk should follow — an index like{' '}
            <code>^NSEI</code>, <code>^GSPC</code> or <code>^FTSE</code> — and it starts on the
            next run. Until then it places no orders and raises no complaints.
          </p>
        </div>
      )}

      {!!rules.retired_keys.length && (
        <p className="meta" style={{ color: 'var(--warning-text)', marginBottom: 12 }}>
          Still stored under an old name: <code>{rules.retired_keys.join(', ')}</code>. The desk
          reads them, so nothing is lost. Saving here writes them under their current names.
        </p>
      )}

      {error && <div className="error" style={{ marginBottom: 12 }}>{error}</div>}

      {!open ? (
        <div className="card">
          <div className="meta-row">
            <span>Trading calendar</span>
            <span className="mono">{values.calendar_symbol || 'not set'}</span>
          </div>
          <div className="meta-row"><span>Clock</span><span className="mono">{values.market_tz}</span></div>
          <div className="meta-row">
            <span>Ticker suffix</span>
            <span className="mono">{values.symbol_suffix || 'none'}</span>
          </div>
          <div className="meta-row">
            <span>Currency</span>
            <span className="mono">{values.currency || 'not set'}</span>
          </div>
          <div className="meta-row">
            <span>Financial year starts</span>
            <span>{MONTHS[values.fy_start_month - 1] ?? values.fy_start_month}</span>
          </div>
          <div className="meta-row">
            <span>Tax rates</span>
            <span className="mono">
              {taxRows.length
                ? taxRows.map(([c, r]) => `${c} ${(r * 100).toFixed(0)}%`).join(', ')
                : 'none — no tax is deducted'}
            </span>
          </div>
        </div>
      ) : (
        <>
          <div className="grid">
            <div className="card">
              <h3>The market</h3>
              <Field
                label="Trading calendar"
                hint="The instrument whose trading days are the market's days, in Yahoo's naming. Leave it empty and the desk does nothing."
              >
                <input
                  type="text" style={BOX} placeholder="^NSEI"
                  value={values.calendar_symbol}
                  onChange={e => set('calendar_symbol', e.target.value)}
                />
              </Field>
              <Field label="Clock" hint="The timezone the desk reads today's date from, e.g. Asia/Kolkata or America/New_York.">
                <input
                  type="text" style={BOX} placeholder="UTC"
                  value={values.market_tz}
                  onChange={e => set('market_tz', e.target.value)}
                />
              </Field>
              <Field
                label="Ticker suffix"
                hint="What the price source adds to a plain symbol to say which exchange it trades on: .NS for the NSE, .L for London. US listings need none."
              >
                <input
                  type="text" style={BOX} placeholder=".NS"
                  value={values.symbol_suffix}
                  onChange={e => set('symbol_suffix', e.target.value)}
                />
              </Field>
              <Field label="Currency" hint="The three-letter code every figure on this desk is in.">
                <input
                  type="text" style={BOX} placeholder="INR" maxLength={3}
                  value={values.currency}
                  onChange={e => set('currency', e.target.value.toUpperCase())}
                />
              </Field>
              <Field label="Financial year starts in" hint="Used to group realised gains for tax. January is the calendar year.">
                <select
                  style={BOX} value={values.fy_start_month}
                  onChange={e => set('fy_start_month', Number(e.target.value))}
                >
                  {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
                </select>
              </Field>
            </div>

            <div className="card">
              <h3>The money</h3>
              <Field label="Capital" hint="What the desk started with. Everything it holds is sized against this.">
                <input
                  type="number" style={BOX} min={0} step="any"
                  value={values.capital} onChange={e => num('capital')(e.target.value)}
                />
              </Field>
              <Field label="Flat charge on a sell" hint="A fixed amount your broker takes on every sale, on top of the percentage. 0 if there is none.">
                <input
                  type="number" style={BOX} min={0} step="any"
                  value={values.sell_charge} onChange={e => num('sell_charge')(e.target.value)}
                />
              </Field>
              <Field label="Benchmark" hint="The instrument the desk is meant to beat. Its month-by-month gap to this is the score.">
                <input
                  type="text" style={BOX} placeholder="SHARIABEES.NS"
                  value={values.benchmark} onChange={e => set('benchmark', e.target.value)}
                />
              </Field>
              <Field label="Second benchmark, for context" hint="Shown beside the first. Usually the broad market index.">
                <input
                  type="text" style={BOX} placeholder="^NSEI"
                  value={values.context_benchmark} onChange={e => set('context_benchmark', e.target.value)}
                />
              </Field>
              <Field
                label="Excess return the backtest promised, a year"
                hint="A fraction: 0.06 means six percent a year above the benchmark. The desk complains once a month while it is two standard errors below this."
              >
                <input
                  type="number" style={BOX} min={-1} max={1} step="any"
                  value={values.expected_excess_pa}
                  onChange={e => num('expected_excess_pa')(e.target.value)}
                />
              </Field>
            </div>

            <div className="card">
              <h3>The tax model</h3>
              <p className="meta" style={{ marginBottom: 10 }}>
                Used for one figure: what the desk would owe if it sold everything today. With
                no rates set it deducts nothing and says so. Rates are fractions, so 0.2 is 20%.
              </p>
              <Field label="Short-term rate, per asset class" hint="A gain held less than a year. An asset class with no rate of its own pays the highest one here.">
                <>
                  {taxRows.map(([cls, rate], i) => (
                    <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6, maxWidth: 320 }}>
                      <input
                        type="text" style={{ flex: 2 }} placeholder="equity" value={cls}
                        onChange={e => setTax(taxRows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
                      />
                      <input
                        type="number" style={{ flex: 1 }} min={0} max={1} step="any" value={rate}
                        onChange={e => setTax(taxRows.map((r, j) => (j === i ? [r[0], Number(e.target.value)] : r)))}
                      />
                      <button className="btn btn-sm" onClick={() => setTax(taxRows.filter((_, j) => j !== i))}>
                        ✕
                      </button>
                    </div>
                  ))}
                  {!taxRows.length && <div className="empty">No rates. No tax is deducted.</div>}
                  <button className="btn btn-sm" style={{ marginTop: 4 }} onClick={() => setTax([...taxRows, ['', 0]])}>
                    + Add an asset class
                  </button>
                </>
              </Field>
              <Field label="Long-term rate" hint="A gain held more than a year, whatever the asset class.">
                <input
                  type="number" style={BOX} min={0} max={1} step="any"
                  value={values.long_term_rate} onChange={e => num('long_term_rate')(e.target.value)}
                />
              </Field>
              <Field label="Long-term gains left untaxed each year" hint="An amount, not a fraction. 0 if your law has no such allowance.">
                <input
                  type="number" style={BOX} min={0} step="any"
                  value={values.long_term_exemption} onChange={e => num('long_term_exemption')(e.target.value)}
                />
              </Field>
              <Field label="Which classes get that allowance" hint="Comma separated. India's covers listed equity and equity-oriented units, so a gold ETF is left out.">
                <input
                  type="text" style={BOX} placeholder="equity"
                  value={values.long_term_exemption_classes.join(', ')}
                  onChange={e =>
                    set('long_term_exemption_classes',
                      e.target.value.split(',').map(s => s.trim()).filter(Boolean))
                  }
                />
              </Field>
            </div>

            <div className="card">
              <h3>When to worry</h3>
              <Field
                label="Days before the trading calendar looks wrong"
                hint="A long weekend plus a holiday is 4 to 5 days in most markets. Raise it if yours closes for longer, or the desk will complain every year."
              >
                <input
                  type="number" style={BOX} min={1} max={60}
                  value={values.stale_calendar_days}
                  onChange={e => num('stale_calendar_days')(e.target.value)}
                />
              </Field>
              <Field
                label="Days before a price is too old to act on"
                hint="Past this the desk leaves a holding out of its sizing rather than buying or selling at a stale price."
              >
                <input
                  type="number" style={BOX} min={1} max={60}
                  value={values.stale_price_days}
                  onChange={e => num('stale_price_days')(e.target.value)}
                />
              </Field>
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <button className="btn btn-primary" disabled={saving} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save the market settings'}
            </button>
            {saved && (
              <span className="meta" style={{ marginLeft: 12 }}>
                Saved. The desk reads these on its next run — no restart needed.
              </span>
            )}
          </div>
        </>
      )}
    </section>
  );
}
