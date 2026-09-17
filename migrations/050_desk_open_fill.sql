-- The trading desk fills at the open, and its schedule moves to market time
-- (spec docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md §3, §6).
--
-- The desk filled an order at the CLOSE of its fill day, which put a full
-- session between a signal taken from the previous close and the price the desk
-- claimed for it. Filling at the open is the earliest price actually obtainable
-- from that signal, and it stops an order sitting pending overnight.
--
-- Three statements, all idempotent.
--
-- WHICH WAY IT FAILS IN THE WINDOW BETWEEN THE NEW IMAGE STARTING AND THIS
-- RUNNING: the `open` column is what the new code reads, and `_bars` selects it
-- by name, so a run before statement 1 lands would error rather than mis-price.
-- Core applies migrations under an advisory lock before it serves anything and
-- the worker's first desk tick is minutes away at the earliest, so the window is
-- not reachable in practice. It fails closed either way: `fill_at` defaults to
-- 'close' in code, so a desk that somehow runs before statement 2 behaves
-- exactly as it did yesterday rather than filling at a price it cannot read.

-- 1. Where the open is kept. Same rule as `close`: the first value seen for a
-- day wins, because Yahoo rewrites past prices after a split. Nullable, because
-- every row written before today has no open and must not be invented.
ALTER TABLE finance.desk_prices ADD COLUMN IF NOT EXISTS open numeric;

-- 1b. Which print a fill actually got. `price_source` says WHERE the price came
-- from (yahoo or ansaar) and is compared by equality in the month summary and
-- the admin page, so it cannot carry this too.
--
-- This exists because the open has a fallback: an order whose open never
-- arrives fills at that day's close instead of being cancelled. Without a
-- column saying which happened, a desk quietly filling every order at the close
-- would look exactly like one filling at the open, and the fallback would never
-- be seen firing. NULL on every row written before this, which is honest — they
-- were all closes, but none of them recorded it.
ALTER TABLE finance.desk_orders ADD COLUMN IF NOT EXISTS price_kind text
    CHECK (price_kind IN ('open', 'close'));

-- 2. Fill at the open. Written explicitly rather than left to the code default,
-- so the admin Trading desk page shows what this desk actually does, and so
-- flipping back is a config edit rather than a deploy. `defaults || config`
-- lets an operator who has already set it win.
UPDATE activities
SET config = jsonb_build_object('fill_at', 'open') || config,
    updated_at = now()
WHERE slug = 'trading-desk-daily'
  AND NOT (config ? 'fill_at');

-- 3. Three fires a day, written in the market's own clock.
--
-- 08:00 plans (before the exchange opens, which is what makes the open a price
-- the desk could actually have paid), 11:00 fills at that open, 14:00 retries
-- anything a source outage left unfilled.
--
-- The zone prefix is read by schedule_sync and passed to Temporal as the
-- schedule's timezone. It matters beyond readability: the desk reads "today"
-- from `market_tz`, and the pre-open planning guard depends on these fires
-- straddling 09:15 MARKET time, so the trigger and the guard have to be on one
-- clock.
--
-- Guarded on the exact previous value, so a cron already edited by hand on
-- /admin/flows is left alone — crons are DB-owned once seeded.
UPDATE activities
SET schedule_cron = 'CRON_TZ=Asia/Kolkata 0 8,11,14 * * 1-5',
    updated_at = now()
WHERE slug = 'trading-desk-daily'
  AND schedule_cron = '30 2 * * 1-5';
