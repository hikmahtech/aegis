import { useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';

const NAV = [
  { path: '/', label: 'Overview' },
  { path: '/interactions', label: 'Interactions' },
  { path: '/workflows', label: 'Workflows' },
  { path: '/flows', label: 'Flows & Config' },
  { path: '/models', label: 'Models & Providers' },
  { path: '/integrations', label: 'Integrations' },
  { path: '/resources', label: 'Resources' },
  { path: '/personalities', label: 'Personalities' },
  { path: '/market', label: 'Market' },
  { path: '/chat', label: 'Chat' },
  { path: '/knowledge', label: 'Knowledge' },
  { path: '/references', label: 'References' },
  { path: '/content', label: 'Content' },
  { path: '/infra', label: 'Infrastructure' },
  { path: '/audit', label: 'Audit Log' },
  { path: '/admin/homelab', label: 'Homelab' },
  { path: '/admin/money', label: 'Money' },
  { path: '/admin/todoist', label: 'Todoist' },
];

export default function Layout() {
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const closeSidebar = () => setSidebarOpen(false);

  return (
    <>
      <div className="mobile-header">
        <button className="hamburger" onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button>
        <span style={{ fontWeight: 600 }}>AEGIS v3</span>
        <span />
      </div>

      <div className={`sidebar-overlay ${sidebarOpen ? 'open' : ''}`} onClick={closeSidebar} />

      <div className="app-layout">
        <nav className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
          <div className="sidebar-header">
            <h2>AEGIS v3</h2>
          </div>
          {NAV.map(({ path, label }) => (
            <Link
              key={path}
              to={path}
              className={path === '/' ? (location.pathname === '/' ? 'active' : '') : location.pathname.startsWith(path) ? 'active' : ''}
              onClick={closeSidebar}
            >
              {label}
            </Link>
          ))}
        </nav>

        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </>
  );
}
