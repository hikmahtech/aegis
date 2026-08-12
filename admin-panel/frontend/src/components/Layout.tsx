import { useCallback, useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { api, clearCredentials } from '../api/client';
import { NAV } from '../nav';
import Icon from './icons';
import CommandPalette from './CommandPalette';
import Toaster from './Toast';

const logout = () => { clearCredentials(); window.location.reload(); };

type Theme = 'light' | 'dark';

// The no-flash inline script in index.html owns the initial value; this only
// reads back what it (or the OS) settled on.
function readTheme(): Theme {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'light' || explicit === 'dark') return explicit;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function Layout() {
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [pending, setPending] = useState(0);
  // Browser-facing URLs for external service links (Postiz etc.).
  const [extLinks, setExtLinks] = useState<any>({});

  useEffect(() => { api.getTemporalConfig().then(setExtLinks).catch(() => {}); }, []);

  // Pending decisions are the one number worth surfacing everywhere.
  useEffect(() => {
    let alive = true;
    const tick = () => api.overviewBrief()
      .then(b => { if (alive) setPending(Number(b?.pending_interactions) || 0); })
      .catch(() => {});
    void tick();
    const id = setInterval(tick, 60_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  useEffect(() => {
    document.title = pending > 0 ? `(${pending}) AEGIS` : 'AEGIS';
  }, [pending]);

  const toggleTheme = useCallback(() => {
    setTheme(prev => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('aegis-theme', next); } catch { /* private mode */ }
      return next;
    });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setCmdOpen(o => !o);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const closeSidebar = () => setSidebarOpen(false);
  const isActive = (path: string) =>
    path === '/' ? location.pathname === '/' : location.pathname.startsWith(path);

  return (
    <>
      <div className={`sidebar-overlay ${sidebarOpen ? 'open' : ''}`} onClick={closeSidebar} />

      <div className="app-layout">
        <nav className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
          <div className="sidebar-header">
            <span className="brand-dot"><Icon name="shield" /></span>
            <h2>AEGIS</h2>
          </div>
          {NAV.map(({ section, items }) => (
            <div className="nav-section" key={section}>
              <div className="nav-section-label">{section}</div>
              {items.map(({ path, label, icon }) => (
                <Link
                  key={path}
                  to={path}
                  className={isActive(path) ? 'active' : ''}
                  onClick={closeSidebar}
                >
                  <Icon name={icon} />
                  <span className="nav-label">{label}</span>
                  {path === '/interactions' && pending > 0 && (
                    <span className="nav-badge">{pending}</span>
                  )}
                </Link>
              ))}
              {section === 'Configure' && extLinks.postiz_ui_url && (
                <a
                  href={extLinks.postiz_ui_url}
                  target="_blank"
                  rel="noopener"
                  onClick={closeSidebar}
                >
                  <Icon name="external" />
                  <span className="nav-label">Postiz</span>
                </a>
              )}
            </div>
          ))}

          <div className="sidebar-footer">
            <button className="sidebar-action" onClick={() => { closeSidebar(); logout(); }}>
              <Icon name="logout" />
              <span>Log out</span>
            </button>
          </div>
        </nav>

        <main className="main-content">
          <div className="topbar">
            <button
              className="icon-btn topbar-hamburger"
              onClick={() => setSidebarOpen(o => !o)}
              aria-label="Toggle navigation"
            >
              <Icon name="menu" />
            </button>
            <button className="topbar-search" onClick={() => setCmdOpen(true)}>
              <Icon name="search" />
              <span>Search…</span>
              <span className="kbd kbd-hint">⌘K</span>
            </button>
            <span className="topbar-spacer" />
            <button
              className="icon-btn"
              onClick={toggleTheme}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              title={theme === 'dark' ? 'Light theme' : 'Dark theme'}
            >
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
            </button>
          </div>

          <div className="page-body">
            <Outlet />
          </div>
        </main>
      </div>

      {cmdOpen && (
        <CommandPalette
          onClose={() => setCmdOpen(false)}
          onToggleTheme={toggleTheme}
          onLogout={logout}
        />
      )}
      <Toaster />
    </>
  );
}
