export function Sparkline({ values, height = 28 }: { values: number[]; height?: number }) {
  const max = Math.max(1, ...values)

  return (
    <div className="flex items-end gap-0.5" style={{ height }}>
      {values.length === 0 && (
        <span className="text-xs text-muted-foreground">No data yet</span>
      )}
      {values.map((v, i) => {
        const pct = Math.max(4, Math.round((v / max) * 100))
        const recent = i >= values.length - 3
        return (
          <div
            key={i}
            className={`flex-1 rounded-t-sm transition-all ${recent ? "bg-green-500" : "bg-green-500/50"}`}
            style={{ height: `${pct}%` }}
            title={v.toFixed(1)}
          />
        )
      })}
    </div>
  )
}
