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
      background: '#fee', border: '1px solid #f88', color: '#900',
      padding: '10px 14px', margin: '8px 0', borderRadius: 4,
      display: 'flex', justifyContent: 'space-between', alignItems: 'center'
    }}>
      <span>{msg}</span>
      <button onClick={() => { setVisible(false); onDismiss?.(); }}
        style={{ background: 'transparent', border: 'none', color: '#900', cursor: 'pointer', fontSize: 18 }}>×</button>
    </div>
  );
}
