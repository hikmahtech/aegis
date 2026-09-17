// The labelled field the config panels share.
//
// `Field` and `BOX` were byte-identical copies in ChartPanel and
// DeskRulesPanel — two forms that sit next to each other on the Money page, so
// a change to one and not the other shows as two field styles on one screen.

/** One labelled field, with the sentence that says what it is for. */
export function Field({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 14 }}>
      <span style={{ display: 'block', fontWeight: 600, marginBottom: 2 }}>{label}</span>
      <span className="meta" style={{ display: 'block', marginBottom: 4 }}>{hint}</span>
      {children}
    </label>
  );
}

/** The width every input in those forms takes. */
export const BOX: React.CSSProperties = { width: '100%', maxWidth: 320 };
