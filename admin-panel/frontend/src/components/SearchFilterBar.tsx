import { useState } from 'react';
import type { FormEvent } from 'react';

export interface FilterField {
  name: string;
  label: string;
  type?: 'text' | 'select';
  options?: Array<{ value: string; label: string }>;
  placeholder?: string;
}

interface Props {
  fields: FilterField[];
  initial?: Record<string, string>;
  onSearch: (values: Record<string, string>) => void;
  submitLabel?: string;
}

export default function SearchFilterBar({ fields, initial = {}, onSearch, submitLabel = 'Search' }: Props) {
  const [values, setValues] = useState<Record<string, string>>(initial);

  function update(name: string, v: string) {
    setValues(s => ({ ...s, [name]: v }));
  }
  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    onSearch(values);
  }

  return (
    <form onSubmit={handleSubmit} className="filter-bar" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
      {fields.map(f => (
        <label key={f.name} style={{ display: 'flex', flexDirection: 'column', fontSize: 12 }}>
          <span>{f.label}</span>
          {f.type === 'select' ? (
            <select value={values[f.name] || ''} onChange={e => update(f.name, e.target.value)}>
              <option value="">—</option>
              {f.options?.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          ) : (
            <input
              type="text"
              value={values[f.name] || ''}
              placeholder={f.placeholder}
              onChange={e => update(f.name, e.target.value)}
            />
          )}
        </label>
      ))}
      <button type="submit" className="btn">{submitLabel}</button>
    </form>
  );
}
