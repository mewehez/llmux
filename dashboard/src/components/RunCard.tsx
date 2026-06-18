import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import type { LoadTaskStatus, LoadTestRun } from "@/lib/types"

const STATUS_CLASS: Record<LoadTaskStatus, string> = {
  pending:   "border-slate-400 text-slate-500",
  streaming: "border-yellow-500 text-yellow-600",
  done:      "border-green-500 text-green-600",
  error:     "border-destructive text-destructive",
  timeout:   "border-orange-500 text-orange-600",
}

const BAR_CLASS: Record<LoadTaskStatus, string> = {
  pending:   "bg-slate-400/50",
  streaming: "bg-yellow-500",
  done:      "bg-green-500",
  error:     "bg-destructive",
  timeout:   "bg-orange-500",
}

export function RunCard({ run }: { run: LoadTestRun }) {
  const total = run.tasks.length
  const done = run.tasks.filter(t => t.status === "done")
  const errors = run.tasks.filter(t => t.status === "error").length
  const timeouts = run.tasks.filter(t => t.status === "timeout").length
  const finished = run.tasks.filter(t => t.status !== "pending" && t.status !== "streaming")
  const avgTps = done.length
    ? done.reduce((sum, t) => sum + (t.tokensPerSec ?? 0), 0) / done.length
    : 0
  const maxElapsed = Math.max(1, ...run.tasks.map(t => t.elapsedMs ?? 0))
  const allFinished = finished.length === total

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-2">
          <span>{run.model === "auto" ? "Auto" : `SmolLM2-${run.model}`}</span>
          <span className="font-mono text-xs text-muted-foreground">{run.time}</span>
        </CardTitle>
        <div className="flex items-center gap-2 text-xs">
          <Badge variant="outline" className={done.length === total ? "border-green-500 text-green-600" : ""}>
            {done.length}/{total} success
          </Badge>
          {errors > 0 && <Badge variant="outline" className="border-destructive text-destructive">{errors} error</Badge>}
          {timeouts > 0 && <Badge variant="outline" className="border-orange-500 text-orange-600">{timeouts} timeout</Badge>}
          {allFinished && (
            <span className="text-muted-foreground">avg {avgTps.toFixed(1)} tok/s</span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Per-task timing bars — scrollable, ~5 rows tall so big runs stay compact. */}
        <div className="max-h-28 space-y-1.5 overflow-y-auto pr-1">
          {run.tasks.map(task => (
            <div key={task.id} className="flex items-center gap-2 text-xs">
              <span className="w-16 shrink-0 font-mono text-muted-foreground">{task.id}</span>
              <div className="h-3 flex-1 overflow-hidden rounded-sm bg-muted">
                <div
                  className={`h-full rounded-sm transition-all ${BAR_CLASS[task.status]}`}
                  style={{ width: `${task.elapsedMs ? Math.max(2, (task.elapsedMs / maxElapsed) * 100) : 0}%` }}
                />
              </div>
              <span className="w-12 shrink-0 text-right tabular-nums text-muted-foreground">
                {task.elapsedMs != null ? `${(task.elapsedMs / 1000).toFixed(1)}s` : "—"}
              </span>
            </div>
          ))}
        </div>

        <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
          {run.tasks.map(task => (
            <div key={task.id} className="rounded-md border bg-muted/30 p-2 text-xs">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="truncate font-mono text-muted-foreground">
                  {task.id}
                  {task.workerId && ` · worker ${task.workerId}`}
                  {task.llmPod && ` → ${task.llmPod}`}
                </span>
                <Badge variant="outline" className={STATUS_CLASS[task.status]}>{task.status}</Badge>
              </div>
              <p className="font-medium">{task.prompt}</p>
              {task.response && (
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{task.response}</p>
              )}
              {task.status === "error" && (
                <p className="mt-1 text-destructive">Request failed before completion.</p>
              )}
              {task.status === "timeout" && (
                <p className="mt-1 text-orange-600">Timed out waiting for response.</p>
              )}
              {task.tokensPerSec != null && (
                <p className="mt-1 tabular-nums text-muted-foreground">{task.tokens} tok · {task.tokensPerSec.toFixed(1)} tok/s</p>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
