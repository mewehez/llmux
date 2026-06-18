import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ModelSelect } from "@/components/ModelSelect"
import { Sparkline } from "@/components/Sparkline"
import { useTimeseries } from "@/lib/metrics"
import type { ModelSelection } from "@/lib/types"

export interface LoadTestSummary {
  count:      number
  model:      ModelSelection
  done:       number
  errors:     number
  avgElapsed: number // ms
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card size="sm">
      <CardContent>
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
        <p className="text-xl font-bold tabular-nums">{value}</p>
        {sub && <p className="text-[10px] text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  )
}

export function MetricsPanel({ lastLoadTest }: { lastLoadTest: LoadTestSummary | null }) {
  const [model, setModel] = useState<ModelSelection>("135m")
  const { data, isLive } = useTimeseries(model === "auto" ? "135m" : model)

  const tokensSeries = data?.series.map(p => p.tokens) ?? []

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">Model</span>
        <ModelSelect value={model} onChange={setModel} />
        <Badge
          variant="outline"
          className={`ml-auto ${isLive ? "border-green-500 text-green-600" : "border-border text-muted-foreground"}`}
        >
          {isLive ? "live" : "no data"}
        </Badge>
      </div>

      {/* Real percentiles + rates, computed server-side over a rolling window. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <StatCard label="p50 latency" value={`${data?.latency.p50 ?? 0}ms`} />
        <StatCard label="p95 latency" value={`${data?.latency.p95 ?? 0}ms`} />
        <StatCard label="p99 latency" value={`${data?.latency.p99 ?? 0}ms`} />
        <StatCard label="decode tok/s" value={`${data?.tokSec.avg ?? 0}`} sub="avg" />
        <StatCard label="req rate" value={`${data?.requestRate ?? 0}/s`} sub={`${data?.count ?? 0} in ${data?.window ?? 0}s`} />
        <StatCard label="slots" value={`${data?.activeSlots ?? 0}/${data?.maxSlots ?? 0}`} sub={`queue ${data?.queueDepth ?? 0}`} />
      </div>

      <Card>
        <CardHeader><CardTitle>Throughput — tokens completed per second (rolling {data?.window ?? 0}s)</CardTitle></CardHeader>
        <CardContent>
          {tokensSeries.length === 0 ? (
            <p className="py-6 text-center text-sm italic text-muted-foreground">No completions in the window yet.</p>
          ) : (
            <Sparkline values={tokensSeries} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Last load test</CardTitle></CardHeader>
        <CardContent>
          {!lastLoadTest ? (
            <p className="text-sm italic text-muted-foreground">No load test run yet.</p>
          ) : (
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div><p className="text-muted-foreground">Requests</p><p className="text-lg font-semibold tabular-nums">{lastLoadTest.count}</p></div>
              <div><p className="text-muted-foreground">Model</p><p className="text-lg font-semibold">{lastLoadTest.model === "auto" ? "Auto" : `SmolLM2-${lastLoadTest.model}`}</p></div>
              <div><p className="text-muted-foreground">Done</p><p className="text-lg font-semibold tabular-nums text-green-600">{lastLoadTest.done}</p></div>
              <div><p className="text-muted-foreground">Errors</p><p className={`text-lg font-semibold tabular-nums ${lastLoadTest.errors > 0 ? "text-destructive" : ""}`}>{lastLoadTest.errors}</p></div>
              <div className="col-span-2"><p className="text-muted-foreground">Avg elapsed</p><p className="text-lg font-semibold tabular-nums">{(lastLoadTest.avgElapsed / 1000).toFixed(1)}s</p></div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
