-- The trading desk's market moves from Python into its own config
-- (spec docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md §12).
--
-- The desk used to have one operator's market compiled in: an Indian clock, an
-- NSE calendar, a ".NS" ticker suffix, an April financial year and India's
-- capital gains rates. Those are now settings, and the code defaults name no
-- market at all — so a desk with no `calendar_symbol` does nothing rather than
-- guessing someone else's holidays.
--
-- That leaves a live desk holding paper positions against a row written before
-- any of these keys existed. This migration gives it exactly the values the
-- code used to hardcode, so the day after the deploy is arithmetically the same
-- as the day before, and then renames the three keys that carried a currency in
-- their name.
--
-- Both statements are idempotent. The first fills only keys the row does not
-- already have (`defaults || config` lets the row win). The second does nothing
-- once the old names are gone. `Rules.from_config` reads the old names too, so
-- a deployment is correct from the moment the code lands, whether or not this
-- has run yet.

-- 1. The market the desk was already trading, said out loud.
UPDATE activities
SET config = jsonb_build_object(
        'calendar_symbol', '^NSEI',
        'market_tz', 'Asia/Kolkata',
        'symbol_suffix', '.NS',
        'currency', 'INR',
        'fy_start_month', 4,
        'stale_calendar_days', 6,
        'stale_price_days', 7,
        'long_term_exemption_classes', jsonb_build_array('equity')
    ) || config,
    updated_at = now()
WHERE slug = 'trading-desk-daily'
  AND NOT (config ?& array[
        'calendar_symbol', 'market_tz', 'symbol_suffix', 'currency',
        'fy_start_month', 'stale_calendar_days', 'stale_price_days',
        'long_term_exemption_classes']);

-- 2. The three renamed keys. The stored value moves to the new name; a row
-- that somehow has both keeps the new one. `jsonb_strip_nulls` means a key
-- that was never set is dropped rather than written as a null.
UPDATE activities
SET config = (config - 'sell_charge_inr' - 'ltcg_rate' - 'ltcg_exemption_inr')
    || jsonb_strip_nulls(jsonb_build_object(
        'sell_charge', COALESCE(config -> 'sell_charge', config -> 'sell_charge_inr'),
        'long_term_rate', COALESCE(config -> 'long_term_rate', config -> 'ltcg_rate'),
        'long_term_exemption', COALESCE(config -> 'long_term_exemption', config -> 'ltcg_exemption_inr')
    )),
    updated_at = now()
WHERE slug = 'trading-desk-daily'
  AND config ?| array['sell_charge_inr', 'ltcg_rate', 'ltcg_exemption_inr'];
