-- What each LLM call cost, as the LiteLLM proxy priced it.
--
-- AEGIS deliberately keeps no price list of its own. The proxy already prices
-- every model it serves — Bedrock included — and returns the number on the
-- response, so the number stored here is the one the thing that placed the
-- call computed, not one AEGIS re-derived from a table it would have to keep
-- in step with five providers.
--
-- NULL means "not priced", which is different from zero: a backend that is
-- not the LiteLLM proxy returns no cost header, and a free local model
-- returns a real 0.0. A query that treats NULL as 0 is under-reporting;
-- `count(*) FILTER (WHERE cost_usd IS NULL)` is how you see the gap.
--
-- numeric, not float: money in a float accumulates error over the millions of
-- rows this table grows to, and a per-call cost of 9.6e-07 needs the scale.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cost_usd numeric(14, 8);

-- The spend queries are "by model / purpose over a window", so the window
-- leads. Partial: an unpriced row contributes nothing to a spend total.
CREATE INDEX IF NOT EXISTS llm_calls_cost_window
    ON llm_calls (created_at DESC) WHERE cost_usd IS NOT NULL;
