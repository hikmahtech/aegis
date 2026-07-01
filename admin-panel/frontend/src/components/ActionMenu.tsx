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
        onClick={() => setOpen(o => !o)}
        style={{ background: 'transparent', border: '1px solid #ccc', borderRadius: 4, padding: '2px 8px', cursor: 'pointer' }}
      >⋮</button>
      {open && (
        <>
          <div
            onClick={() => setOpen(false)}
            style={{ position: 'fixed', inset: 0, zIndex: 9 }}
          />
          <div style={{
            position: 'absolute', right: 0, top: '100%', background: '#fff',
            border: '1px solid #ccc', borderRadius: 4, zIndex: 10, minWidth: 180,
            boxShadow: '0 2px 6px rgba(0,0,0,0.1)',
          }}>
            {items.map((it, i) => (
              <button key={i} onClick={() => run(it)} style={{
                display: 'block', width: '100%', textAlign: 'left', padding: '6px 12px',
                background: 'transparent', border: 'none',
                color: it.destructive ? '#c00' : 'inherit', cursor: 'pointer',
              }}>{it.label}</button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
