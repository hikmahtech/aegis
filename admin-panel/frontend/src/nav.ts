import type { IconName } from './components/icons';

// Single source of truth for navigation. Consumed by the sidebar and the ⌘K
// palette, so a new page is added in exactly one place.
export type NavItem = { path: string; label: string; icon: IconName };
export type NavSection = { section: string; items: NavItem[] };

export const NAV: NavSection[] = [
  {
    section: 'Operate',
    items: [
      { path: '/', label: 'Overview', icon: 'overview' },
      { path: '/interactions', label: 'Interactions', icon: 'inbox' },
      { path: '/workflows', label: 'Workflows', icon: 'workflows' },
      { path: '/chat', label: 'Chat', icon: 'chat' },
    ],
  },
  {
    section: 'Knowledge & Data',
    items: [
      { path: '/knowledge', label: 'Knowledge', icon: 'knowledge' },
      { path: '/references', label: 'References', icon: 'references' },
      { path: '/content', label: 'Content', icon: 'content' },
      { path: '/people', label: 'People', icon: 'people' },
      { path: '/expiring-items', label: 'Expiry Radar', icon: 'expiry' },
      { path: '/assets', label: 'Assets', icon: 'assets' },
      { path: '/market', label: 'Market', icon: 'market' },
      { path: '/admin/money', label: 'Money', icon: 'money' },
    ],
  },
  {
    section: 'Configure',
    items: [
      { path: '/agents', label: 'Agents', icon: 'agents' },
      { path: '/flows', label: 'Flows', icon: 'flows' },
      { path: '/models', label: 'Models', icon: 'models' },
      { path: '/integrations', label: 'Integrations', icon: 'integrations' },
      { path: '/channels', label: 'Channels', icon: 'channels' },
      { path: '/slack', label: 'Slack', icon: 'slack' },
      { path: '/resources', label: 'Resources', icon: 'resources' },
    ],
  },
  {
    section: 'System',
    items: [
      { path: '/infra', label: 'Infrastructure', icon: 'infra' },
      { path: '/system', label: 'System monitoring', icon: 'monitoring' },
      { path: '/admin/todoist', label: 'Todoist', icon: 'todoist' },
      { path: '/admin/email-triage', label: 'Email triage', icon: 'email' },
      { path: '/audit', label: 'Audit', icon: 'audit' },
      { path: '/settings', label: 'Settings', icon: 'settings' },
    ],
  },
];
