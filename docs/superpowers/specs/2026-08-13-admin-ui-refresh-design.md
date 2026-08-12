# Admin panel UI/UX refresh — design

Date: 2026-08-13
Status: approved

## Problem

The admin SPA (`admin-panel/frontend`, 30 pages / ~8,700 lines) works but reads as
unfinished. Three concrete causes, all confirmed by inspection rather than taste:

1. **No contrast structure.** Sidebar is `#ffffff`, cards are `#ffffff`, the page canvas
   is `#f7f8fa`, borders are `#eaecf0`. Every surface melts into the next, so the eye
   gets no hierarchy and the whole app reads flat.
2. **Zero icons.** `grep -r "svg\|Icon" src/pages src/components` returns nothing. The
   sidebar is 25 bare text links in 4 groups. This is the single largest driver of the
   "plain" impression.
3. **No type scale.** Default system font stack, and effectively one size — `0.85rem`
   grey — used for body, table cells, card text and meta alike.

Secondary: no dark mode, no motion, `className="loading"` renders the literal string
"Loading…" in 26 places, the tab title is `frontend`, and the login page is an unstyled
inline-styled box.

## Constraints

- **No page rewrites.** All 30 pages already share one class vocabulary (`.card`,
  `.data-table`, `.badge`, `.btn`, `.stat-item`, …), so a token-layer rewrite lifts
  every page at once. Page files are edited only where they hardcode a color or render
  a status message.
- **No new npm dependencies.** The SPA has 4 runtime deps; it keeps 4.
- **Dark mode must be token-only.** Audit found ~15 real hardcoded color literals across
  all pages — small enough to convert, which is what makes token-driven theming viable.

## Design

### 1. Token layer (`src/index.css`, rewritten)

Same token *names* as today (so nothing downstream breaks), new values:

- **Neutrals** re-ramped to give surfaces separation: canvas deeper, cards white and
  floating, sidebar on its own tone, borders with real definition.
- **Type scale** — `--fs-xs` … `--fs-2xl` tokens, applied to the existing primitives so
  page code inherits the scale without changing.
- **Elevation** — shadows re-tuned per surface role rather than one flat `--shadow-sm`
  everywhere.
- **Focus** — a visible `:focus-visible` ring on every interactive primitive
  (accessibility basic; currently absent).

### 2. Dark mode

Tokens redefined in two guarded blocks:

```
:root                                              /* light, complete palette */
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }
:root[data-theme="dark"] { … }
```

Explicit choice is stamped on `<html data-theme>` and persisted to `localStorage`;
default follows the OS. The ~15 hardcoded literals in page files are swapped to tokens
so they theme correctly.

### 3. Icons (`src/components/icons.tsx`, new)

~30 inline SVG paths, 24×24, stroke-based, `currentColor`. Exported as
`<Icon name="…" />`. Hand-written rather than pulling `lucide-react`: the set is fixed
and small, and the dependency tree stays untouched.

### 4. Shell (`src/components/Layout.tsx`)

- Brand mark + wordmark.
- Icon nav; active state is a tinted pill with a left accent rail.
- A slim sticky top bar inside the content column carrying the ⌘K trigger, the theme
  toggle and the pending-decision badge.
- Sidebar footer: theme toggle, log out.

Pages continue to own their own `<h1 className="page-title">` — the top bar does not
duplicate it, so no page needs editing.

### 5. Command palette (`src/components/CommandPalette.tsx`, new)

⌘K / Ctrl-K opens a modal that filters the navigation routes by substring, with
arrow-key navigation and Enter to route. The nav list moves out of `Layout.tsx` into
`src/nav.ts` so Layout and the palette share one source.

Scope: routes plus two actions (toggle theme, log out). Agents and flows are
deliberately excluded — including them requires API calls and a loading state for a
list that is already one click away.

### 6. Pending-decision badge

`Layout` polls `api.overviewBrief()` every 60s and renders `pending_interactions` as a
badge on the Interactions nav item and as a `(n)` prefix in `document.title`.

### 7. Toasts (`src/components/Toast.tsx`, new)

Module-level emitter (`toast.ok()` / `toast.err()`) plus a `<Toaster/>` mounted once in
`Layout`. The 17 existing inline `msg-success` / `msg-error` render sites are converted
to call it.

### 8. Skeletons

`.loading` is restyled globally from a text string into a spinner + label — this covers
all 26 usages with no page edits. A `.skeleton` shimmer utility is added and applied to
the three highest-traffic list surfaces (Overview, Interactions, Workflows).

### 9. Polish

- `index.html`: real `<title>` and a favicon matching the brand mark.
- `Login.tsx`: rebuilt as a proper centred auth card using the design system instead of
  inline styles.

## Out of scope

Tailwind / shadcn adoption, page rewrites, new npm dependencies, and a self-hosted web
font (the font stack gains `system-ui` and names Inter, using it only where already
installed).

## Verification

- `npm run build` (tsc + vite) passes.
- `npm run lint` passes.
- Every route renders in both themes with no unstyled or invisible text.
- No page loses functionality: the only page-file edits are color literals and status
  messages.
