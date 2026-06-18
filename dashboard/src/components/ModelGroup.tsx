import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { ReplicaCard } from "@/components/ReplicaCard"
import type { ModelSlots } from "@/lib/api"
import type { Model, WorkerMetrics } from "@/lib/types"

function CountBadge({ n, max }: { n: number; max: number }) {
  return (
    <Badge variant="outline" className={n >= max ? "border-green-500 text-green-600" : ""}>
      {n}/{max}
    </Badge>
  )
}

export function ModelGroup({
  model,
  replicas,
  maxReplicas,
  queueDepth,
  slots,
}: {
  model: Model
  replicas: WorkerMetrics[]
  maxReplicas: number
  queueDepth: number
  slots?: ModelSlots
}) {
  const pods = slots?.pods ?? []
  const maxLlmPods = slots?.maxLlmPods ?? Math.max(1, pods.length)

  return (
    <div className="space-y-4">
      <h2 className="font-mono text-sm font-semibold">SmolLM2-{model}</h2>

      {/* Model-level work queue (what's waiting to be picked up by any worker) */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between text-xs uppercase tracking-wider text-muted-foreground">
            <span>Redis queue (llm:work:{model})</span>
            <span className="tabular-nums">{queueDepth}</span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Progress value={Math.min((queueDepth / 20) * 100, 100)} indicatorClassName="bg-yellow-500" />
        </CardContent>
      </Card>

      {/* ── Tier 1: llama.cpp pods (scale independently; RAM-bound) ── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between text-xs uppercase tracking-wider text-muted-foreground">
            <span>LLM pods (llama.cpp) — requests in flight</span>
            <CountBadge n={pods.length} max={maxLlmPods} />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {pods.length === 0 && (
            <p className="text-sm italic text-muted-foreground">No llama.cpp pods reporting.</p>
          )}
          {pods.map(p => {
            const inPod = p.processing + p.deferred  // requests held by this pod
            return (
              <div key={p.pod} className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs">{p.pod}</span>
                  <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
                    {p.processing}/{p.total} slots
                    {p.deferred > 0 && <span className="ml-1 text-yellow-600">+{p.deferred} queued</span>}
                  </span>
                </div>
                {/* parallel slots (= --parallel); deferred (queued in llama.cpp) shows yellow */}
                <Progress
                  value={p.total ? Math.min((inPod / p.total) * 100, 100) : 0}
                  indicatorClassName={p.deferred > 0 ? "bg-yellow-500" : "bg-blue-500"}
                />
                {p.processing > p.total && (
                  <span className="text-[10px] tabular-nums text-muted-foreground">+{p.processing - p.total} over capacity</span>
                )}
              </div>
            )
          })}
        </CardContent>
      </Card>

      {/* ── Tier 2: workers (stateless; scale on queue depth) ── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between text-xs uppercase tracking-wider text-muted-foreground">
            <span>Workers — requests sent</span>
            <CountBadge n={replicas.length} max={maxReplicas} />
          </CardTitle>
        </CardHeader>
        <CardContent>
          {replicas.length === 0 && (
            <p className="text-sm italic text-muted-foreground">No workers reporting.</p>
          )}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {replicas.map(r => <ReplicaCard key={r.workerId} replica={r} />)}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
