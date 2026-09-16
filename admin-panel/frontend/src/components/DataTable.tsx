import type { HTMLAttributes, Key, ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react';

// The `data-table` skeleton, written once.
//
// Thirty-odd tables in this panel spell out the same thead/tbody/empty-row
// shape, and the part that differs is only ever the columns. Written out per
// page they drift: a table forgets its empty row and shows a blank slab
// instead of saying why, or its `colSpan` stops matching its column count and
// the empty message sits in the first column.
//
// A column says what its header is and how to render one row's cell; `th` and
// `td` carry the odd width or `className` the old markup had, so the rendered
// HTML — and every CSS class in it — is unchanged.

export type Column<T> = {
  header?: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  /** Attributes for this column's `<th>` — a width, an alignment. */
  th?: ThHTMLAttributes<HTMLTableCellElement>;
  /** Attributes for its `<td>`; a function when they depend on the row. */
  td?:
    | TdHTMLAttributes<HTMLTableCellElement>
    | ((row: T, index: number) => TdHTMLAttributes<HTMLTableCellElement>);
};

type Props<T> = {
  columns: Column<T>[];
  rows: T[];
  /** Shown in one full-width row when there is nothing to list. */
  emptyText?: ReactNode;
  rowKey?: (row: T, index: number) => Key;
  tr?: (row: T, index: number) => HTMLAttributes<HTMLTableRowElement>;
  className?: string;
};

export default function DataTable<T>({
  columns,
  rows,
  emptyText,
  rowKey,
  tr,
  className = 'data-table',
}: Props<T>) {
  return (
    <table className={className}>
      <thead>
        <tr>
          {columns.map((c, i) => (
            <th key={i} {...c.th}>{c.header}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={rowKey ? rowKey(row, i) : i} {...(tr ? tr(row, i) : {})}>
            {columns.map((c, j) => (
              <td key={j} {...(typeof c.td === 'function' ? c.td(row, i) : c.td)}>
                {c.cell(row, i)}
              </td>
            ))}
          </tr>
        ))}
        {rows.length === 0 && emptyText != null && (
          <tr><td colSpan={columns.length} className="empty">{emptyText}</td></tr>
        )}
      </tbody>
    </table>
  );
}
