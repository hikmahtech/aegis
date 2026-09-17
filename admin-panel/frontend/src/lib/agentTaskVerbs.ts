// Source tag → the agent-task lane's verb (`agent_task_verbs`, #558).
//
// Each tag has three states on the page, and the row stores only two of them:
//   DEFAULT — no entry in the row; the tag keeps its code default
//   NONE    — an explicit null: "nothing here works these, leave them to me"
//   a verb  — reroute the tag to that verb
// So a select value of DEFAULT drops the tag from the saved overrides, and
// NONE saves `null`. Keeping that mapping here (and tested) is what stops a
// "(default)" choice being saved as a real override that pins today's value.

export const DEFAULT = '__default__';
export const NONE = '__none__';

export type Verb = string | null;
export type VerbRow = { tag: string; choice: string };

export function choiceOf(tag: string, overrides: Record<string, Verb>): string {
  if (!(tag in overrides)) return DEFAULT;
  const v = overrides[tag];
  return v === null ? NONE : v;
}

// One row per tag the page should show: every default tag, then any tag only
// the overrides name, in that order.
export function toRows(
  defaults: Record<string, Verb>,
  overrides: Record<string, Verb>,
): VerbRow[] {
  const tags = [...Object.keys(defaults), ...Object.keys(overrides).filter(t => !(t in defaults))];
  return tags.map(tag => ({ tag, choice: choiceOf(tag, overrides) }));
}

// The overrides to PUT. A blank tag is skipped; DEFAULT is not an override.
export function toOverrides(rows: VerbRow[]): Record<string, Verb> {
  const out: Record<string, Verb> = {};
  for (const { tag, choice } of rows) {
    const t = tag.trim();
    if (!t || choice === DEFAULT) continue;
    out[t] = choice === NONE ? null : choice;
  }
  return out;
}

// What a tag does with the current choice, for the "effect" column.
export function describe(choice: string, fallback: Verb | undefined): string {
  if (choice === NONE) return 'Left to you: the task parks with a note.';
  if (choice === DEFAULT) {
    if (fallback === undefined) return 'No default: the task parks as undecided.';
    return fallback === null ? 'Default: left to you.' : `Default: ${fallback}.`;
  }
  return `Runs the ${choice} verb.`;
}
