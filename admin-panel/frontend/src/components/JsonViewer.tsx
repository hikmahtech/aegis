interface Props { data: unknown; }
export default function JsonViewer({ data }: Props) {
  return (
    <pre style={{
      background: '#f5f5f5', padding: 12, borderRadius: 4,
      fontSize: 12, overflowX: 'auto', maxHeight: 400,
    }}>{JSON.stringify(data, null, 2)}</pre>
  );
}
