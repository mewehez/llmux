import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import type { WorkerMetrics } from "@/lib/types"

const STATUS_VARIANT: Record<WorkerMetrics["status"], { label: string; className: string }> = {
  healthy:  { label: "healthy",  className: "border-green-500 text-green-600" },
  busy:     { label: "busy",     className: "border-yellow-500 text-yellow-600" },
  draining: { label: "draining", className: "border-purple-500 text-purple-600" },
  down:     { label: "down",     className: "border-destructive text-destructive" },
  starting: { label: "starting", className: "border-blue-500 text-blue-600" },
}

function Stat({ value, label, className }: { value: number | string; label: string; className?: string }) {
  return (
    <div>
      <p className={`text-sm font-semibold tabular-nums ${className ?? ""}`}>{value}</p>
      <p className="text-[10px] uppercase text-muted-foreground">{label}</p>
    </div>
  )
}

// A worker is a stateless consumer: it pulls jobs from the Redis stream and
// calls the runner. We surface what it's doing right now (in-flight + live
// tok/s) alongside the work it has done.
export function ReplicaCard({ replica }: { replica: WorkerMetrics }) {
  const status = STATUS_VARIANT[replica.status]
  const inflight = replica.inflight ?? 0
  const liveTokS = replica.liveTokS ?? 0
  const lastMs = Math.round(replica.lastLatencyMs ?? replica.latencyP50)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <p className="truncate font-mono text-sm font-semibold">{replica.workerId}</p>
        <Badge variant="outline" className={status.className}>{status.label}</Badge>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="grid grid-cols-4 gap-2 text-center">
          <Stat value={replica.totalReqs} label="done" />
          <Stat value={inflight} label="in-flight" className={inflight > 0 ? "text-yellow-600" : ""} />
          <Stat value={Math.round(liveTokS)} label="tok/s now" />
          <Stat value={replica.errors} label="errors" className={replica.errors > 0 ? "text-destructive" : ""} />
        </div>
        <p className="text-center text-[10px] text-muted-foreground">last request {lastMs}ms</p>
      </CardContent>
    </Card>
  )
}
