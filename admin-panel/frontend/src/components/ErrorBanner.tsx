import { useState, useEffect } from 'react';

interface Props {
  error: Error | string | null;
  onDismiss?: () => void;
}

export default function ErrorBanner({ error, onDismiss }: Props) {
  const [visible, setVisible] = useState(true);
  useEffect(() => { setVisible(true); }, [error]);
  if (!error || !visible) return null;
  const msg = typeof error === 'string' ? error : (error.message || 'Unknown error');
  return (
    <div style={{
      background: 'var(--danger-tint)', border: '1px solid #fecdca', color: 'var(--danger-text)',
      padding: '10px 14px', margin: '8px 0', borderRadius: 'var(--radius-sm)',
      display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12,
    }}>
      <span>{msg}</span>
      <button onClick={() => { setVisible(false); onDismiss?.(); }}
        style={{ background: 'transparent', border: 'none', color: 'var(--danger-text)', cursor: 'pointer', fontSize: 18, lineHeight: 1 }}>×</button>
    </div>
  );
}
