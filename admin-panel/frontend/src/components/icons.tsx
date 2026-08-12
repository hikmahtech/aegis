import type { JSX } from 'react';

// Hand-rolled 24×24 stroke icons. Deliberately not a dependency: the set is
// fixed and small, and the SPA keeps its 4 runtime deps.
//
// Geometry is built from primitives (line/rect/circle/polyline/path) so every
// glyph stays legible at the 16px the sidebar renders them at.

const P: Record<string, JSX.Element> = {
  // ── Navigation ──────────────────────────────────────────────────────────
  overview: <><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>,
  inbox: <><path d="M4 13h4l1.5 3h5L16 13h4" /><path d="M5.5 5h13l2.5 8v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5z" /></>,
  workflows: <><circle cx="6" cy="5" r="2.5" /><circle cx="18" cy="12" r="2.5" /><circle cx="6" cy="19" r="2.5" /><path d="M6 7.5v9" /><path d="M8.5 5H13a2.5 2.5 0 0 1 2.5 2.5v2" /><path d="M8.5 19H13a2.5 2.5 0 0 0 2.5-2.5v-2" /></>,
  chat: <><path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z" /></>,
  knowledge: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z" /><path d="M4 18.5A2.5 2.5 0 0 1 6.5 21H19" /><path d="M8.5 7.5h6" /><path d="M8.5 11h4" /></>,
  references: <><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4.5L5 21V4a1 1 0 0 1 1-1z" /></>,
  content: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><polyline points="14 3 14 8 19 8" /><path d="M9 13h6" /><path d="M9 17h4" /></>,
  people: <><circle cx="9" cy="8" r="3.2" /><path d="M3 20a6 6 0 0 1 12 0" /><path d="M16.5 5.5a3.2 3.2 0 0 1 0 6" /><path d="M18 14.5a6 6 0 0 1 3 5.5" /></>,
  expiry: <><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2" /><path d="M9 2h6" /></>,
  assets: <><path d="M12 2.5 21 7v10l-9 4.5L3 17V7z" /><path d="M3 7l9 4.5L21 7" /><path d="M12 11.5V21" /></>,
  market: <><polyline points="3 17 9 11 13 15 21 7" /><polyline points="21 12 21 7 16 7" /></>,
  money: <><rect x="2.5" y="6" width="19" height="13" rx="2.5" /><path d="M2.5 10.5h19" /><path d="M6.5 15h3" /></>,
  agents: <><rect x="4" y="7.5" width="16" height="12" rx="3" /><circle cx="9" cy="13.5" r="1.15" fill="currentColor" stroke="none" /><circle cx="15" cy="13.5" r="1.15" fill="currentColor" stroke="none" /><path d="M12 7.5V4" /><circle cx="12" cy="3" r="1.3" /></>,
  flows: <><polygon points="13 2 4.5 13.5 11 13.5 10 22 19 10 12.5 10" /></>,
  models: <><path d="M12 3.5a4 4 0 0 0-4 4v.4a3.6 3.6 0 0 0 0 6.9v1.2a4 4 0 0 0 8 0v-1.2a3.6 3.6 0 0 0 0-6.9v-.4a4 4 0 0 0-4-4z" /><path d="M12 3.5v17" /></>,
  integrations: <><path d="M9 2v5" /><path d="M15 2v5" /><rect x="6" y="7" width="12" height="7" rx="2.5" /><path d="M12 14v3a4 4 0 0 0 4 4h1" /></>,
  channels: <><path d="M5 9h15" /><path d="M4 15h15" /><path d="M11 3.5 9 20.5" /><path d="M16 3.5 14 20.5" /></>,
  slack: <><rect x="3" y="10" width="7" height="4" rx="2" /><rect x="10" y="3" width="4" height="7" rx="2" /><rect x="14" y="10" width="7" height="4" rx="2" /><rect x="10" y="14" width="4" height="7" rx="2" /></>,
  resources: <><ellipse cx="12" cy="6" rx="8" ry="3.2" /><path d="M4 6v12c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2V6" /><path d="M4 12c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2" /></>,
  infra: <><rect x="3" y="3.5" width="18" height="7" rx="2" /><rect x="3" y="13.5" width="18" height="7" rx="2" /><path d="M7 7h.01" /><path d="M7 17h.01" /></>,
  monitoring: <><path d="M2.5 12h4l2.5-7 4 14 2.5-7h6" /></>,
  todoist: <><rect x="3.5" y="3.5" width="17" height="17" rx="4" /><polyline points="8 12.2 11 15 16.5 9.5" /></>,
  email: <><rect x="2.5" y="5" width="19" height="14" rx="2.5" /><polyline points="3.5 7 12 13 20.5 7" /></>,
  audit: <><path d="M12 2.5 4.5 5.5v6c0 4.6 3.1 8.7 7.5 10 4.4-1.3 7.5-5.4 7.5-10v-6z" /><polyline points="9 12 11.3 14.2 15.3 10" /></>,
  settings: <><circle cx="12" cy="12" r="3.2" /><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.2 5.2l2.1 2.1M16.7 16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1" /></>,

  // ── UI ──────────────────────────────────────────────────────────────────
  shield: <><path d="M12 2.5 4.5 5.5v6c0 4.6 3.1 8.7 7.5 10 4.4-1.3 7.5-5.4 7.5-10v-6z" /></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5 21 21" /></>,
  sun: <><circle cx="12" cy="12" r="4.2" /><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7" /></>,
  moon: <><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" /></>,
  menu: <><path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17" /></>,
  close: <><path d="M6 6l12 12M18 6L6 18" /></>,
  logout: <><path d="M15 4.5h3a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-3" /><polyline points="10 8 14 12 10 16" /><path d="M14 12H3.5" /></>,
  arrowRight: <><path d="M4.5 12h15" /><polyline points="13.5 6 19.5 12 13.5 18" /></>,
  external: <><path d="M14 4h6v6" /><path d="M20 4l-8.5 8.5" /><path d="M18 14v4.5a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2H10" /></>,
  check: <><circle cx="12" cy="12" r="9" /><polyline points="8 12.2 11 15 16 9.5" /></>,
  alert: <><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5" /><path d="M12 16.4h.01" /></>,
  enter: <><polyline points="9 10 5 14 9 18" /><path d="M5 14h10a4 4 0 0 0 4-4V6" /></>,
};

export type IconName = keyof typeof P;

export default function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      {P[name]}
    </svg>
  );
}
