-- The books' entities and chart of accounts move from Python into a settings
-- row (issue #560).
--
-- The money lane used to have one operator's accounting compiled in: two
-- entities named `personal` and `hikmah`, the `:hikmah:` account segment that
-- told them apart, both unknown-account pairs, and every category → account
-- mapping. Those are now `settings.books_chart`, read on every post, and the
-- code default (`books_chart.DEFAULT_CHART`) names nobody: one entity,
-- `personal`, with no segment and the generic categories.
--
-- This migration gives a live deployment exactly the chart the code used to
-- hardcode, so the day after the deploy posts the same accounts as the day
-- before.
--
-- WHICH WAY IT FAILS IN THE WINDOW BETWEEN THE NEW IMAGE STARTING AND THIS
-- RUNNING: a personal transaction is unaffected — `DEFAULT_CHART` carries the
-- same categories and the same `expenses:unknown` / `income:unknown` pair, so
-- it posts identically. A `hikmah` transaction resolves to the default entity,
-- which files it under `expenses:unknown` rather than
-- `expenses:hikmah:unknown` — visible in the digest and reclassifiable, never
-- lost. In practice the window is empty: Core runs migrations at startup,
-- under an advisory lock, before it serves a request or the worker posts
-- anything through it.
--
-- Idempotent, and written so a re-run is a no-op — a migration is keyed on its
-- FILENAME, so renaming or renumbering this makes it run again. `DO NOTHING`
-- also means an operator who has already edited the chart from the admin panel
-- keeps their edit.

INSERT INTO settings (key, value, updated_at)
VALUES (
    'books_chart',
    jsonb_build_object(
        'default_entity', 'personal',
        'income_categories', jsonb_build_array('interest', 'refund', 'salary'),
        'entities', jsonb_build_object(
            -- The default entity: no segment, so it owns every expense and
            -- income account no other entity's segment claims. That is exactly
            -- what `account_entity`'s `else "personal"` branch did.
            'personal', jsonb_build_object(
                'label', 'Personal',
                'segment', '',
                'unknown', jsonb_build_object(
                    'in', 'income:unknown',
                    'out', 'expenses:unknown'
                ),
                'categories', jsonb_build_object(
                    'saas', 'expenses:saas',
                    'media', 'expenses:media',
                    'infra', 'expenses:saas',
                    'internet', 'expenses:utilities:internet',
                    'electricity', 'expenses:utilities:electricity',
                    'mobile', 'expenses:utilities:mobile',
                    'groceries', 'expenses:groceries',
                    'food', 'expenses:food',
                    'transport', 'expenses:transport',
                    'shopping', 'expenses:shopping',
                    'health', 'expenses:health',
                    'insurance', 'expenses:insurance',
                    'fees', 'expenses:fees:bank',
                    'tax', 'expenses:tax',
                    'people', 'expenses:people',
                    'salary', 'income:salary',
                    'interest', 'income:interest',
                    'refund', 'income:refunds'
                )
            ),
            -- The business entity. Its segment is `hikmah`, which is the
            -- literal `":hikmah:" in f"{account}:"` test the code carried.
            -- Its unknown-IN is `income:hikmah:other`, which is what the old
            -- `account_for` returned for every credit on this side — it has no
            -- income categories, so `Chart.account_for` reaches the same
            -- account by the general rule.
            'hikmah', jsonb_build_object(
                'label', 'Hikmah',
                'segment', 'hikmah',
                'unknown', jsonb_build_object(
                    'in', 'income:hikmah:other',
                    'out', 'expenses:hikmah:unknown'
                ),
                'categories', jsonb_build_object(
                    'saas', 'expenses:hikmah:saas',
                    'media', 'expenses:hikmah:saas',
                    'infra', 'expenses:hikmah:infra',
                    'internet', 'expenses:hikmah:internet',
                    'fees', 'expenses:hikmah:fees:bank',
                    'tax', 'expenses:hikmah:tax',
                    'professional', 'expenses:hikmah:professional',
                    'ads', 'expenses:hikmah:ads'
                )
            )
        )
    ),
    now()
)
ON CONFLICT (key) DO NOTHING;
