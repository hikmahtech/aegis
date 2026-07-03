import { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { api, clearCredentials } from '../api/client';

const logout = () => { clearCredentials(); window.location.reload(); };

// Grouped navigation. Labels are display-only; routes are unchanged.
const NAV: { section: string; items: { path: string; label: string }[] }[] = [
  {
    section: 'Operate',
    items: [
      { path: '/', label: 'Overview' },
      { path: '/interactions', label: 'Interactions' },
      { path: '/workflows', label: 'Workflows' },
      { path: '/chat', label: 'Chat' },
    ],
  },
  {
    section: 'Knowledge & Data',
    items: [
      { path: '/knowledge', label: 'Knowledge' },
      { path: '/references', label: 'References' },
      { path: '/content', label: 'Content' },
      { path: '/market', label: 'Market' },
      { path: '/admin/money', label: 'Money' },
    ],
  },
  {
    section: 'Configure',
    items: [
      { path: '/agents', label: 'Agents' },
      { path: '/flows', label: 'Flows' },
      { path: '/models', label: 'Models' },
      { path: '/integrations', label: 'Integrations' },
      { path: '/channels', label: 'Channels' },
      { path: '/slack', label: 'Slack' },
      { path: '/resources', label: 'Resources' },
    ],
  },
  {
    section: 'System',
    items: [
      { path: '/infra', label: 'Infrastructure' },
      { path: '/system', label: 'System monitoring' },
      { path: '/admin/todoist', label: 'Todoist' },
      { path: '/audit', label: 'Audit' },
      { path: '/settings', label: 'Settings' },
    ],
  },
];

export default function Layout() {
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Browser-facing URLs for external service links (Postiz etc.).
  const [extLinks, setExtLinks] = useState<any>({});
  useEffect(() => { api.getTemporalConfig().then(setExtLinks).catch(() => {}); }, []);

  const closeSidebar = () => setSidebarOpen(false);
  const isActive = (path: string) =>
    path === '/' ? location.pathname === '/' : location.pathname.startsWith(path);

  return (
    <>
      <div className="mobile-header">
        <button className="hamburger" onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button>
        <span style={{ fontWeight: 700 }}>AEGIS</span>
        <span />
      </div>

      <div className={`sidebar-overlay ${sidebarOpen ? 'open' : ''}`} onClick={closeSidebar} />

      <div className="app-layout">
        <nav className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
          <div className="sidebar-header">
            <span className="brand-dot" />
            <h2>AEGIS</h2>
          </div>
          {NAV.map(({ section, items }) => (
            <div className="nav-section" key={section}>
              <div className="nav-section-label">{section}</div>
              {items.map(({ path, label }) => (
                <Link
                  key={path}
                  to={path}
                  className={isActive(path) ? 'active' : ''}
                  onClick={closeSidebar}
                >
                  {label}
                </Link>
              ))}
              {section === 'Configure' && extLinks.postiz_ui_url && (
                <a
                  href={extLinks.postiz_ui_url}
                  target="_blank"
                  rel="noopener"
                  onClick={closeSidebar}
                >
                  Postiz ↗
                </a>
              )}
            </div>
          ))}
          <a onClick={() => { closeSidebar(); logout(); }} style={{ cursor: 'pointer', marginTop: 'auto' }}>
            Log out
          </a>
        </nav>

        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </>
  );
}
