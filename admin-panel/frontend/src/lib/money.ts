/**
 * The TypeScript half of `fmt_money` (core/src/aegis/services/money_format.py).
 *
 * It lives in its own module so it can be unit-tested: the two implementations
 * have to agree, and until issue #386 nothing in CI would have noticed them
 * drifting apart. `money.test.ts` is that agreement, written down.
 */

const MONEY_SYMBOL: Record<string, string> = { INR: '₹', USD: '$', GBP: '£', EUR: '€' };

const _groupers: Record<string, Intl.NumberFormat> = {};
function grouper(locale: string): Intl.NumberFormat {
  let f = _groupers[locale];
  if (!f) {
    f = new Intl.NumberFormat(locale, { useGrouping: true });
    _groupers[locale] = f;
  }
  return f;
}

/**
 * Sign, then symbol, then grouped digits — Indian grouping for INR
 * (₹1,00,308.53), thousands elsewhere, ISO code as a suffix when there is no
 * symbol.
 *
 * `amount` is the STRING the API sends, and it is never re-rounded here. The
 * server quantizes a Python `Decimal` with ROUND_HALF_UP; JavaScript has no
 * decimal type, so a `Number(...)` + `toFixed(2)` round-trip would round a
 * binary float and eventually disagree with the ledger on a half-paisa. There
 * is one rounding authority and it is the server's — this function only groups
 * digits it was handed. Grouping comes from `Intl.NumberFormat` on a `BigInt`
 * (exact at any size) rather than hand-sliced digits.
 */
export function fmtMoney(
  amount: string | null | undefined,
  currency: string | null | undefined,
): string {
  if (amount === null || amount === undefined || amount === '') return '';
  const raw = String(amount).trim();
  const m = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(raw);
  // Anything that is not a plain decimal is shown verbatim: inventing a
  // formatted number for input we did not understand is worse than the raw
  // string, on a page whose whole job is to agree with the journal.
  if (!m || (!m[2] && !m[3])) return raw;
  const whole = (m[2] || '0').replace(/^0+(?=\d)/, '');
  // `numeric(14,2)` means two decimals arrive already rounded; a longer
  // fraction is truncated rather than rounded, so this never becomes a second
  // rounding authority.
  const frac = (m[3] || '').padEnd(2, '0').slice(0, 2);
  // "-0.00" is not negative — Python's Decimal comparison agrees, and a
  // leading minus on a zero would be a difference the reader cannot explain.
  const isZero = !/[1-9]/.test(whole + frac);
  const sign = m[1] === '-' && !isZero ? '-' : '';
  const code = (currency || '').toUpperCase();
  const grouped = grouper(code === 'INR' ? 'en-IN' : 'en-US').format(BigInt(whole));
  const sym = MONEY_SYMBOL[code];
  if (sym) return `${sign}${sym}${grouped}.${frac}`;
  if (code) return `${sign}${grouped}.${frac} ${code}`;
  return `${sign}${grouped}.${frac}`;
}
