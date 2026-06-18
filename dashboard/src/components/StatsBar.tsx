import { Card, CardContent } from "@/components/ui/card"
import type { WorkerMetrics } from "@/lib/types"

function Stat({ label, value, className }: { label: string; value: number | string; className?: string }) {
  return (
    <Card className="flex-1">
      <CardContent className="py-3">
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
        <p className={`text-xl font-bold tabular-nums ${className ?? ""}`}>{value}</p>
      </CardContent>
    </Card>
  )
}

export function StatsBar({ workers, queued, scaleEvents }: { workers: WorkerMetrics[]; queued: number; scaleEvents: number }) {
  const total    = workers.reduce((a, w) => a + w.totalReqs, 0)
  const active   = workers.reduce((a, w) => a + w.activeSlots, 0)
  // `queued` is the sum of DISTINCT model queue depths (passed in) — NOT summed
  // per worker, since every worker of a model reports that model's whole queue.
  const errors   = workers.reduce((a, w) => a + w.errors, 0)
  const healthy  = workers.filter(w => w.healthy).length

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <Stat label="Total requests" value={total} />
      <Stat label="Active" value={active} className="text-yellow-600" />
      <Stat label="Queued" value={queued} className={queued > 0 ? "text-yellow-600" : ""} />
      <Stat label="Errors" value={errors} className={errors > 0 ? "text-destructive" : ""} />
      <Stat label="Workers healthy" value={`${healthy}/${workers.length}`} className="text-green-600" />
      <Stat label="Scale events" value={scaleEvents} />
    </div>
  )
}
