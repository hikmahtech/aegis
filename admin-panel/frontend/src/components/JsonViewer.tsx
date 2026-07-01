interface Props { data: unknown; }
export default function JsonViewer({ data }: Props) {
  return (
    <pre style={{
      background: 'var(--surface-2)', border: '1px solid var(--border)',
      padding: 12, borderRadius: 'var(--radius-sm)',
      fontSize: 12, overflowX: 'auto', maxHeight: 400,
      fontFamily: 'var(--mono)', color: 'var(--text)',
    }}>{JSON.stringify(data, null, 2)}</pre>
  );
}
