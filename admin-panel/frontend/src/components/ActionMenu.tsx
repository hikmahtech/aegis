import { useState } from 'react';

export interface ActionItem {
  label: string;
  destructive?: boolean;
  confirm?: string;
  onClick: () => void | Promise<void>;
}

interface Props { items: ActionItem[]; }

export default function ActionMenu({ items }: Props) {
  const [open, setOpen] = useState(false);

  async function run(item: ActionItem) {
    setOpen(false);
    if (item.confirm && !window.confirm(item.confirm)) return;
    await item.onClick();
  }

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        className="btn-icon"
        onClick={() => setOpen(o => !o)}
      >⋮</button>
      {open && (
        <>
          <div
            onClick={() => setOpen(false)}
            style={{ position: 'fixed', inset: 0, zIndex: 9 }}
          />
          <div style={{
            position: 'absolute', right: 0, top: '100%', background: 'var(--surface)',
            border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', zIndex: 10, minWidth: 180,
            boxShadow: 'var(--shadow-md)', overflow: 'hidden', marginTop: 4,
          }}>
            {items.map((it, i) => (
              <button key={i} onClick={() => run(it)} style={{
                display: 'block', width: '100%', textAlign: 'left', padding: '8px 12px',
                background: 'transparent', border: 'none', fontSize: '0.85rem',
                color: it.destructive ? 'var(--danger-text)' : 'var(--text)', cursor: 'pointer',
              }}>{it.label}</button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
