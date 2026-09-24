/**
 * Three small charts for the money pages: a line chart, a grouped bar chart
 * and horizontal bars.
 *
 * ponytail: hand-rolled SVG rather than a chart library. The SPA has four
 * runtime deps and these pages need three shapes; a library is worth it the
 * day a page needs zoom, log axes or a candlestick.
 *
 * Every colour is a CSS token (`var(--accent)`), so dark mode works for free.
 */

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

export type Series = {
  name: string;
  /** A CSS colour, always a token: `var(--accent)`. */
  color: string;
  values: (number | null)[];
  dashed?: boolean;
  /** Shade the area between the line and zero. */
  area?: boolean;
};

const PAD = { top: 12, right: 12, bottom: 24, left: 64 };

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(240, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Four round-ish ticks spanning [lo, hi]. */
function ticks(lo: number, hi: number): number[] {
  if (lo === hi) return [lo];
  const raw = (hi - lo) / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) ?? raw;
  const out: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(t);
  return out;
}

function extent(series: Series[], zero: boolean): [number, number] {
  const all = series.flatMap(s => s.values.filter((v): v is number => v !== null));
  let lo = Math.min(...all, ...(zero ? [0] : []));
  let hi = Math.max(...all, ...(zero ? [0] : []));
  if (!Number.isFinite(lo)) return [0, 1];
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.06;
  return [lo - (zero && lo === 0 ? 0 : pad), hi + pad];
}

export function Legend({ series }: { series: Pick<Series, 'name' | 'color' | 'dashed'>[] }) {
  return (
    <div className="chart-legend">
      {series.map(s => (
        <span key={s.name}>
          <i style={{ borderTopColor: s.color, borderTopStyle: s.dashed ? 'dashed' : 'solid' }} />
          {s.name}
        </span>
      ))}
    </div>
  );
}

/** A tooltip box that follows the hovered index. */
function Tip({ x, width, children }: { x: number; width: number; children: ReactNode }) {
  const left = x > width / 2 ? undefined : x + 12;
  const right = x > width / 2 ? width - x + 12 : undefined;
  return <div className="chart-tip" style={{ left, right }}>{children}</div>;
}

export function LineChart({
  labels, series, fmt, height = 220, zero = false,
}: {
  labels: string[];
  series: Series[];
  fmt: (v: number) => string;
  height?: number;
  /** Always include zero and draw it as the baseline. */
  zero?: boolean;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  if (!labels.length) return <div className="empty">Nothing to draw yet.</div>;
  const [lo, hi] = extent(series, zero);
  const w = width - PAD.left - PAD.right;
  const h = height - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (labels.length === 1 ? w / 2 : (i / (labels.length - 1)) * w);
  const y = (v: number) => PAD.top + h - ((v - lo) / (hi - lo)) * h;
  const path = (vals: (number | null)[]) =>
    vals.map((v, i) => (v === null ? '' : `${i && vals[i - 1] !== null ? 'L' : 'M'}${x(i)},${y(v)}`)).join('');
  const every = Math.max(1, Math.ceil(labels.length / Math.floor(w / 90)));

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    const i = Math.round(((e.clientX - box.left - PAD.left) / w) * (labels.length - 1));
    setHover(Math.min(labels.length - 1, Math.max(0, i)));
  }

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticks(lo, hi).map(t => (
          <g key={t}>
            <line x1={PAD.left} x2={PAD.left + w} y1={y(t)} y2={y(t)} className="chart-grid" />
            <text x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="chart-axis">{fmt(t)}</text>
          </g>
        ))}
        {zero && <line x1={PAD.left} x2={PAD.left + w} y1={y(0)} y2={y(0)} className="chart-zero" />}
        {/* Every `every`-th label plus the last, but never one too close to the
            last: with 8 labels at every=3, "Sep 23" and "Sep 24" printed on top
            of each other. */}
        {labels.map((l, i) => (i === labels.length - 1 || (i % every === 0 && labels.length - 1 - i >= every)) && (
          <text key={l + i} x={x(i)} y={height - 6} className="chart-axis"
            textAnchor={i === labels.length - 1 && labels.length > 1 ? 'end' : 'middle'}>{l}</text>
        ))}
        {series.map(s => s.area && (
          <path
            key={`${s.name}-area`}
            d={`${path(s.values)}L${x(s.values.length - 1)},${y(Math.max(lo, 0))}L${x(0)},${y(Math.max(lo, 0))}Z`}
            fill={s.color}
            opacity={0.12}
          />
        ))}
        {series.map(s => (
          <path
            key={s.name}
            d={path(s.values)}
            fill="none"
            stroke={s.color}
            strokeWidth={2}
            strokeDasharray={s.dashed ? '5 4' : undefined}
            strokeLinejoin="round"
          />
        ))}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + h} className="chart-cross" />
            {series.map(s => s.values[hover] !== null && (
              <circle key={s.name} cx={x(hover)} cy={y(s.values[hover] as number)} r={3.5} fill={s.color} />
            ))}
          </g>
        )}
      </svg>
      {hover !== null && (
        <Tip x={x(hover)} width={width}>
          <strong>{labels[hover]}</strong>
          {series.map(s => (
            <div key={s.name}>
              <i style={{ background: s.color }} />{s.name}
              <span className="mono">{s.values[hover] === null ? '—' : fmt(s.values[hover] as number)}</span>
            </div>
          ))}
        </Tip>
      )}
    </div>
  );
}

export function BarChart({
  labels, series, fmt, height = 220,
}: {
  labels: string[];
  series: Series[];
  fmt: (v: number) => string;
  height?: number;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  if (!labels.length) return <div className="empty">Nothing to draw yet.</div>;
  const [lo, hi] = extent(series, true);
  const w = width - PAD.left - PAD.right;
  const h = height - PAD.top - PAD.bottom;
  const y = (v: number) => PAD.top + h - ((v - lo) / (hi - lo)) * h;
  const band = w / labels.length;
  const bar = Math.min(28, (band * 0.7) / series.length);
  const x0 = (i: number) => PAD.left + i * band + (band - bar * series.length) / 2;

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} onPointerLeave={() => setHover(null)}>
        {ticks(lo, hi).map(t => (
          <g key={t}>
            <line x1={PAD.left} x2={PAD.left + w} y1={y(t)} y2={y(t)} className="chart-grid" />
            <text x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="chart-axis">{fmt(t)}</text>
          </g>
        ))}
        <line x1={PAD.left} x2={PAD.left + w} y1={y(0)} y2={y(0)} className="chart-zero" />
        {labels.map((l, i) => (
          <g key={l + i} onPointerEnter={() => setHover(i)}>
            <rect x={PAD.left + i * band} y={PAD.top} width={band} height={h} fill="transparent"
              className={hover === i ? 'chart-band' : undefined} />
            {series.map((s, j) => {
              const v = s.values[i] ?? 0;
              return (
                <rect key={s.name} x={x0(i) + j * bar} width={bar - 2} rx={2}
                  y={Math.min(y(v), y(0))} height={Math.abs(y(v) - y(0))} fill={s.color} />
              );
            })}
            <text x={PAD.left + i * band + band / 2} y={height - 6} textAnchor="middle" className="chart-axis">{l}</text>
          </g>
        ))}
      </svg>
      {hover !== null && (
        <Tip x={PAD.left + hover * band + band / 2} width={width}>
          <strong>{labels[hover]}</strong>
          {series.map(s => (
            <div key={s.name}>
              <i style={{ background: s.color }} />{s.name}
              <span className="mono">{fmt(s.values[hover] ?? 0)}</span>
            </div>
          ))}
        </Tip>
      )}
    </div>
  );
}

/** Horizontal bars, each scaled to the largest. Plain HTML: text wraps and
 *  stays readable at any width, which SVG text does not. */
export function HBars({
  rows, fmt, color = 'var(--accent)',
}: {
  rows: { label: string; value: number; note?: string }[];
  fmt: (v: number) => string;
  color?: string;
}) {
  if (!rows.length) return <div className="empty">Nothing to show yet.</div>;
  const max = Math.max(...rows.map(r => Math.abs(r.value))) || 1;
  return (
    <div className="hbars">
      {rows.map(r => (
        <div className="hbar" key={r.label}>
          <span className="hbar-label mono" title={r.label}>{r.label}</span>
          <span className="hbar-track">
            <span style={{ width: `${(Math.abs(r.value) / max) * 100}%`, background: color }} />
          </span>
          <span className="hbar-value mono">{fmt(r.value)}{r.note && <em> · {r.note}</em>}</span>
        </div>
      ))}
    </div>
  );
}

/** One bar split into shares that add up to the whole — an allocation. */
export function StackBar({ parts }: { parts: { label: string; share: number; color: string }[] }) {
  return (
    <>
      <div className="stackbar">
        {parts.filter(p => p.share > 0).map(p => (
          <span key={p.label} style={{ width: `${p.share * 100}%`, background: p.color }}
            title={`${p.label} · ${(p.share * 100).toFixed(1)}%`} />
        ))}
      </div>
      <div className="chart-legend">
        {parts.map(p => (
          <span key={p.label}>
            <i className="dot" style={{ background: p.color }} />
            {p.label} <span className="mono meta">{(p.share * 100).toFixed(1)}%</span>
          </span>
        ))}
      </div>
    </>
  );
}
