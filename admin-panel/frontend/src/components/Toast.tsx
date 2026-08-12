/* eslint-disable react-refresh/only-export-components --
   The store and its renderer are one unit; splitting them buys nothing but a
   dev-only fast-refresh nicety. ponytail: one file until it hurts. */
import { useEffect, useState } from 'react';
import Icon from './icons';

// Module-level store so any module can fire a toast without prop-drilling or a
// context provider. One <Toaster/> is mounted in Layout.
type Item = { id: number; kind: 'ok' | 'err'; text: string };

let seq = 0;
let items: Item[] = [];
const listeners = new Set<(v: Item[]) => void>();

const emit = () => listeners.forEach(l => l(items));

export function dismiss(id: number) {
  items = items.filter(t => t.id !== id);
  emit();
}

function push(kind: Item['kind'], text: string) {
  const id = ++seq;
  items = [...items, { id, kind, text }];
  emit();
  setTimeout(() => dismiss(id), kind === 'err' ? 7000 : 3500);
}

export const toast = {
  ok: (text: string) => push('ok', text),
  err: (text: string) => push('err', text),
};

export default function Toaster() {
  const [list, setList] = useState<Item[]>(items);
  useEffect(() => {
    listeners.add(setList);
    return () => { listeners.delete(setList); };
  }, []);

  if (!list.length) return null;
  return (
    <div className="toaster" role="status" aria-live="polite">
      {list.map(t => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <Icon name={t.kind === 'ok' ? 'check' : 'alert'} />
          <span className="toast-text">{t.text}</span>
          <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss">
            <Icon name="close" />
          </button>
        </div>
      ))}
    </div>
  );
}
