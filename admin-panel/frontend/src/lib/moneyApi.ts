/**
 * The read-only money surface: what `/api/admin/money/*` returns, how to fetch
 * it, and how to print a figure.
 *
 * It lives beside the pages rather than inside `api/client.ts` so the money
 * work lands in files of its own — the accounting panel and the trading desk
 * are the only callers, and `apiFetch` is exported for exactly this.
 *
 * Two conventions the server holds to, recorded here because the types are
 * where they bite:
 *
 * * **Ledger amounts are strings.** They come off Postgres `numeric` or out of
 *   hledger, and a JSON number would round-trip them through a binary float
 *   until the page disagreed with the journal on the last paisa. `fmtMoney`
 *   groups the digits it is handed and never re-rounds them.
 * * **Desk figures are numbers, already rounded by the server.** The desk is
 *   float arithmetic by design — it scores a paper portfolio, it does not post
 *   to the ledger — so there is still exactly one rounding authority and it is
 *   still the server's.
 */

import { apiFetch } from '../api/client';
import { fmtMoney } from './money';

// --------------------------------------------------------------- the accounting

/** One `account, balance` pair exactly as hledger rendered it. */
export type BalanceRow = {
  account: string;
  /** hledger's own cell, verbatim. Mixed commodities arrive as "$-40.00, ₹98,765.50". */
  balance: string;
};

export type BalanceReport = { rows: BalanceRow[]; total: string | null };

export type MoneyBalances = {
  as_of: string;
  month_start: string;
  home_currency: string;
  home_symbol: string;
  /** False when the books checkout is missing or hledger refused; `error` says why. */
  books_ok: boolean;
  error: string | null;
  standing: BalanceReport;
  month: BalanceReport;
};

export type Due = {
  message_id: string;
  payee: string | null;
  amount: string | null;
  currency: string | null;
  due_on: string | null;
  kind: string;
  entity: string | null;
  todoist_ref: string | null;
  linked_message_id?: string | null;
  paid_at?: string | null;
};

export type MoneyDues = {
  as_of: string;
  open: Due[];
  overdue_count: number;
  paid_recently: Due[];
  paid_days: number;
  ticked_off_count: number;
};

export type UnknownRow = {
  message_id: string;
  payee: string | null;
  amount: string;
  currency: string | null;
  occurred_on: string | null;
  channel: string | null;
  account: string;
  entity: string | null;
  instrument: string | null;
  journal_file: string | null;
};

export type MoneyUnknowns = {
  days: number;
  since: string;
  limit: number;
  rows: UnknownRow[];
  /** Counted over the whole window, never over the capped `rows` above. */
  totals: { account: string; currency: string | null; count: number; total: string }[];
};

export type StatementRow = {
  statement_id: string;
  period_start: string;
  period_end: string;
  opening_balance: string | null;
  closing_balance: string | null;
  rows: number;
  reconciled_at: string | null;
  matched: number;
  unmatched: number;
  skipped: number;
};

export type StatementAccount = {
  instrument: string;
  statements: StatementRow[];
  missing_months: string[];
  reconciled_through: string | null;
  unmatched: number;
  rows: number;
};

export type MoneyStatements = {
  as_of: string;
  through_month: string;
  accounts: StatementAccount[];
};

// ------------------------------------------------------------- the trading desk

export type DeskPosition = {
  symbol: string;
  asset_class: string;
  qty: number;
  avg_cost: number | null;
  cost: number | null;
  last_close: number | null;
  priced_on: string | null;
  /** False when no price could be found at all — the holding is carried at cost. */
  priced: boolean;
  value: number | null;
  gain: number | null;
  weight: number | null;
};

export type DeskPendingOrder = {
  id: string;
  data_date: string;
  created_day: string;
  symbol: string;
  asset_class: string;
  side: string;
  qty: number;
  ref_price: number | null;
  est_value: number | null;
};

export type DeskFinding = {
  klass?: string;
  subject?: string;
  title?: string;
  payload?: { description?: string };
};

export type DeskPlan = {
  data_date: string;
  outcome: string;
  findings: DeskFinding[];
  skipped: string[];
  planned_at: string;
};

/** `trading_desk.month_summary` — the monthly close's desk section, verbatim. */
export type DeskScore = {
  since: string;
  weeks: number;
  capital: number;
  value: number;
  after_tax: number;
  benchmark: string;
  benchmark_value: number | null;
  context: string;
  context_value: number | null;
  mean_gap: number;
  t: number;
  label: string;
  below_expectation: boolean;
  expected_excess_pa: number;
  holdings: string[];
  cash_pct: number;
  filled: number;
  costs: number;
  cancelled: Record<string, number>;
  held_back: Record<string, number>;
  ansaar_prices: number;
  moves: { symbol: string; day: string; move: number }[];
};

export type DeskProblem = {
  id: string;
  class: string;
  subject: string;
  title: string;
  severity: string;
  status: string;
  occurrences: number;
  first_seen_at: string;
  last_seen_at: string;
  muted_until: string | null;
  todoist_task_id: string | null;
  description: string | null;
};

export type DeskState = {
  as_of: string;
  mode: string;
  capital: number;
  benchmark: string;
  context_benchmark: string;
  value: number | null;
  cash: number | null;
  cash_pct: number | null;
  invested: number | null;
  gain: number | null;
  realised: number | null;
  tax_if_sold_today: number | null;
  positions: DeskPosition[];
  pending: DeskPendingOrder[];
  latest_plan: DeskPlan | null;
  /** Null until the desk's first fill — there is no result to score yet. */
  score: DeskScore | null;
  problems: DeskProblem[];
};

export type DeskOrder = {
  seq: number;
  created_day: string;
  symbol: string;
  asset_class: string;
  side: string;
  qty: number;
  ref_price: number | null;
  status: string;
  fill_date: string | null;
  fill_price: number | null;
  costs: number | null;
  price_source: string | null;
  reason: string | null;
};

export type DeskHistory = {
  limit: number;
  days: (DeskPlan & { orders: DeskOrder[] })[];
};

// ----------------------------------------------------------------- the fetchers

export const moneyApi = {
  balances: () => apiFetch<MoneyBalances>('/api/admin/money/balances'),
  dues: () => apiFetch<MoneyDues>('/api/admin/money/dues'),
  unknowns: (days?: number) =>
    apiFetch<MoneyUnknowns>(`/api/admin/money/unknowns${days ? `?days=${days}` : ''}`),
  statements: () => apiFetch<MoneyStatements>('/api/admin/money/statements'),
  desk: () => apiFetch<DeskState>('/api/admin/money/desk'),
  deskHistory: (limit?: number) =>
    apiFetch<DeskHistory>(`/api/admin/money/desk/history${limit ? `?limit=${limit}` : ''}`),
};

// --------------------------------------------------------------- the formatters

/**
 * A desk figure as money. The server has already rounded every number it
 * sends, so this only hands the digits to `fmtMoney`, which groups them. It is
 * not a second rounding authority.
 */
export function fmtAmount(value: number | null | undefined, currency = 'INR'): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return fmtMoney(value.toFixed(2), currency);
}

/** A ratio (0.7043) as a percentage ("70.4%"). Nothing renders as an em dash. */
export function fmtPct(value: number | null | undefined, places = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(places)}%`;
}

/** The same, with an explicit sign — for a gap that can go either way. */
export function fmtSignedPct(value: number | null | undefined, places = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(places)}%`;
}

/** "2026-07-01" as "July 2026", for a list of months with no statement. */
export function monthName(iso: string): string {
  const d = new Date(`${iso.slice(0, 7)}-01T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

/** Days from `asOf` to `iso`; negative means the date has already passed. */
export function daysAway(iso: string | null, asOf: string): number | null {
  if (!iso) return null;
  const a = Date.parse(`${asOf}T00:00:00Z`);
  const b = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((b - a) / 86_400_000);
}

/** A Todoist task link, but only for a real task id — an `item-…` ref is a
 *  temp id still sitting in the outbox and Todoist has never seen it. */
export function todoistHref(ref: string | null | undefined): string | null {
  return ref && /^\d+$/.test(ref) ? `https://app.todoist.com/app/task/${ref}` : null;
}
