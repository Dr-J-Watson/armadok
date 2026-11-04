// A tiny chart of requests over time, with the 5xx part in the danger colour. Drawn as plain
// SVG with the theme's variables; the numbers are also in the table next to it.
export function Sparkline({ series, label }: { series: number[][]; label: string }) {
  const width = 120
  const height = 28
  const peak = Math.max(1, ...series.map((p) => p[0]))
  const bar = series.length > 0 ? width / series.length : width
  return (
    <svg role="img" aria-label={label} width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {series.map(([total, errors], i) => {
        const h = (total / peak) * (height - 2)
        const e = (errors / peak) * (height - 2)
        return (
          <g key={i}>
            <rect x={i * bar} y={height - h} width={Math.max(1, bar - 1)} height={h} fill="var(--accent)" opacity={0.7} />
            {errors > 0 && <rect x={i * bar} y={height - e} width={Math.max(1, bar - 1)} height={e} fill="var(--danger)" />}
          </g>
        )
      })}
    </svg>
  )
}
