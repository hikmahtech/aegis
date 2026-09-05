/**
 * `fmtMoney` against `fmt_money` (core/src/aegis/services/money_format.py).
 *
 * Two implementations of one format, in two languages, with no shared source
 * of truth between them — so the only thing keeping them together is a table
 * that says what they both produce. Every `expected` in AGREED below was
 * COMPUTED by running the Python function on that exact input, not written by
 * hand, and `tests/core/test_money_format.py` pins the Python side of the same
 * contract. Change one implementation and its table fails; change the format
 * on purpose and you have to update both, which is the point.
 *
 * The DIVERGES block is the other half of the story: three inputs where the
 * two deliberately do NOT agree. They are recorded here so a later reader does
 * not "fix" them into agreement and quietly give the browser a second opinion
 * about rounding.
 */

import { describe, expect, it } from 'vitest';
import { fmtMoney } from './money';

type Case = [amount: string | null | undefined, currency: string | null | undefined, expected: string];

/** Inputs where fmtMoney and Python's fmt_money produce the identical string. */
const AGREED: Case[] = [
  // Indian digit grouping — 2-digit groups above the last 3.
  ['0.00', 'INR', '₹0.00'],
  ['1.00', 'INR', '₹1.00'],
  ['999.99', 'INR', '₹999.99'],
  ['1000.00', 'INR', '₹1,000.00'],
  ['100308.53', 'INR', '₹1,00,308.53'],
  ['12345678.90', 'INR', '₹1,23,45,678.90'],
  // numeric(14,2) at full width — the widest amount the column can hold.
  ['99999999999999.99', 'INR', '₹9,99,99,99,99,99,999.99'],

  // Symbol currencies — thousands grouping, symbol AFTER the sign.
  ['1234567.89', 'USD', '$1,234,567.89'],
  ['0.50', 'GBP', '£0.50'],
  ['-99.99', 'EUR', '-€99.99'],
  ['-4500.00', 'INR', '-₹4,500.00'],

  // ISO code is upper-cased before the symbol lookup.
  ['1000.00', 'inr', '₹1,000.00'],
  ['10.00', 'usd', '$10.00'],

  // No symbol for the code -> ISO suffix, and thousands (not Indian) grouping.
  ['1500.00', 'AED', '1,500.00 AED'],
  ['1500.00', 'JPY', '1,500.00 JPY'],
  ['-1500.00', 'AED', '-1,500.00 AED'],

  // No currency at all -> bare grouped number.
  ['42.00', null, '42.00'],
  ['42.00', '', '42.00'],

  // Nothing to format is "", not "0.00" and not a crash. `MoneyEvent.amount`
  // is nullable: a receipt with no parsable amount is normal input.
  [null, 'INR', ''],
  [undefined, 'INR', ''],
  ['', 'INR', ''],

  // "-0.00" is not negative. A leading minus on a zero is a difference the
  // reader cannot explain, and Python's Decimal comparison agrees.
  ['-0.00', 'INR', '₹0.00'],

  // Fraction shapes the API can legitimately send.
  ['12.5', 'INR', '₹12.50'],
  ['12', 'INR', '₹12.00'],
  ['12.', 'INR', '₹12.00'],
  ['.5', 'INR', '₹0.50'],
  ['000123.45', 'USD', '$123.45'],
  ['+50.00', 'USD', '$50.00'],
  ['  25.00  ', 'USD', '$25.00'],
];

describe('fmtMoney agrees with the server formatter', () => {
  it.each(AGREED)('fmtMoney(%o, %o) === %o', (amount, currency, expected) => {
    expect(fmtMoney(amount, currency)).toBe(expected);
  });

  it('has a table that actually covers the branches', () => {
    // A guard on the guard: a table that lost its INR or its ISO-suffix rows
    // would still pass every case above while protecting nothing.
    const outputs = AGREED.map((c) => fmtMoney(c[0], c[1]));
    expect(outputs.some((o) => o.startsWith('₹'))).toBe(true);
    expect(outputs.some((o) => o.startsWith('-₹'))).toBe(true);
    expect(outputs.some((o) => o.endsWith(' AED'))).toBe(true);
    expect(outputs.some((o) => o === '')).toBe(true);
  });
});

describe('the server is the only rounding authority', () => {
  // This is the property, not a formatting detail. Python quantizes a Decimal
  // with ROUND_HALF_UP; JavaScript has no decimal type, so any rounding done
  // here would be binary-float rounding and would eventually disagree with the
  // ledger on a half-paisa. fmtMoney therefore TRUNCATES the digits it was
  // handed and never rounds — the two lines below are where it deliberately
  // gives a different answer from Python, and that is correct.
  it('truncates a third decimal rather than rounding it', () => {
    expect(fmtMoney('12.999', 'INR')).toBe('₹12.99'); // Python: ₹13.00
    expect(fmtMoney('0.005', 'INR')).toBe('₹0.00'); // Python: ₹0.01
    expect(fmtMoney('1.999999999', 'USD')).toBe('$1.99');
  });

  it('never widens a value it was given', () => {
    // Whatever the input's first two fraction digits are, those are the two
    // digits shown. Stated as a loop so a future "just use toFixed(2)" — the
    // obvious-looking simplification — fails here rather than in production.
    for (const [whole, frac] of [
      ['7', '999'],
      ['0', '996'],
      ['123456', '505'],
      ['9', '95'],
    ]) {
      const out = fmtMoney(`${whole}.${frac}`, 'USD');
      expect(out.endsWith(`.${frac.slice(0, 2)}`)).toBe(true);
    }
  });

  it('groups digits exactly past the float-safe integer range', () => {
    // 2^53 + 1 is not representable as a double: `Number('9007199254740993')`
    // is 9007199254740992. Grouping through BigInt keeps the digits the server
    // sent. Swap the BigInt for a Number and this case reports the wrong
    // amount to the reader with no error anywhere.
    expect(fmtMoney('9007199254740993.01', 'USD')).toBe('$9,007,199,254,740,993.01');
    expect(fmtMoney('9007199254740993.01', 'INR')).toBe('₹9,00,71,99,25,47,40,993.01');
  });
});

describe('input it does not understand is shown verbatim', () => {
  // Python RAISES `InvalidOperation` on all of these. The browser must not:
  // inventing a formatted number for input we did not understand is worse than
  // the raw string, and a throw here takes the whole Money page down.
  it.each([['N/A'], ['abc'], ['1,000.00'], ['.'], ['-'], ['₹500']])(
    'fmtMoney(%o) passes the string through',
    (amount) => {
      expect(fmtMoney(amount, 'INR')).toBe(amount);
    },
  );

  it('trims before deciding, so whitespace is not "not understood"', () => {
    expect(fmtMoney(' 10.00 ', 'INR')).toBe('₹10.00');
    expect(fmtMoney(' N/A ', 'INR')).toBe('N/A');
  });
});
