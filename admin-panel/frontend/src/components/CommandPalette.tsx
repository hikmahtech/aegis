import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { NAV } from '../nav';
import Icon, { type IconName } from './icons';

type Entry = { group: string; label: string; icon: IconName; run: () => void };

// Mounted only while open (Layout guards it), so closing unmounts and the
// query/cursor reset for free — no reset effect needed.
export default function CommandPalette({
  onClose,
  onToggleTheme,
  onLogout,
}: {
  onClose: () => void;
  onToggleTheme: () => void;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [cursor, setCursor] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const entries = useMemo<Entry[]>(() => [
    ...NAV.flatMap(s =>
      s.items.map(i => ({
        group: s.section,
        label: i.label,
        icon: i.icon,
        run: () => navigate(i.path),
      })),
    ),
    { group: 'Actions', label: 'Toggle theme', icon: 'moon' as IconName, run: onToggleTheme },
    { group: 'Actions', label: 'Log out', icon: 'logout' as IconName, run: onLogout },
  ], [navigate, onToggleTheme, onLogout]);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter(e =>
      e.label.toLowerCase().includes(needle) || e.group.toLowerCase().includes(needle),
    );
  }, [q, entries]);

  // Clamp at read time rather than syncing state to a shrinking result set.
  const active = Math.min(cursor, Math.max(0, results.length - 1));

  // Keep the highlighted row in view while arrowing.
  useEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')
      ?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  const choose = (e: Entry | undefined) => { if (!e) return; onClose(); e.run(); };

  const onKeyDown = (ev: React.KeyboardEvent) => {
    const n = results.length;
    if (ev.key === 'ArrowDown') { ev.preventDefault(); setCursor(n ? (active + 1) % n : 0); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); setCursor(n ? (active - 1 + n) % n : 0); }
    else if (ev.key === 'Enter') { ev.preventDefault(); choose(results[active]); }
    else if (ev.key === 'Escape') { ev.preventDefault(); onClose(); }
  };

  let lastGroup = '';
  return (
    <div className="cmdk-overlay" onMouseDown={onClose}>
      <div className="cmdk" onMouseDown={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="cmdk-input-row">
          <Icon name="search" />
          <input
            autoFocus
            className="cmdk-input"
            placeholder="Jump to a page…"
            value={q}
            onChange={e => { setQ(e.target.value); setCursor(0); }}
            onKeyDown={onKeyDown}
          />
        </div>
        <div className="cmdk-list" ref={listRef}>
          {results.length === 0 && <div className="cmdk-empty">No matches.</div>}
          {results.map((e, i) => {
            const header = e.group !== lastGroup ? ((lastGroup = e.group), e.group) : null;
            return (
              <div key={`${e.group}-${e.label}`}>
                {header && <div className="cmdk-group">{header}</div>}
                <button
                  className="cmdk-item"
                  aria-selected={i === active}
                  onMouseMove={() => setCursor(i)}
                  onClick={() => choose(e)}
                >
                  <Icon name={e.icon} />
                  <span>{e.label}</span>
                  {i === active && <span className="cmdk-hint"><Icon name="enter" /></span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="cmdk-footer">
          <span><span className="kbd">↑↓</span> navigate</span>
          <span><span className="kbd">↵</span> open</span>
          <span><span className="kbd">esc</span> close</span>
        </div>
      </div>
    </div>
  );
}
