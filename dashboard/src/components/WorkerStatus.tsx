import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"
const POLL_INTERVAL = 3000

interface LastTask {
  task_id: string
  message: string
}

interface WorkerInfo {
  model:       string
  stream:      string
  queue_depth: number
  pending:     number
  last_task:   LastTask | null
  llm_url:     string
}

function QueueBadge({ depth }: { depth: number }) {
  if (depth === 0)
    return <Badge variant="secondary">idle</Badge>
  if (depth < 5)
    return <Badge variant="outline" className="border-yellow-500 text-yellow-600">{depth} queued</Badge>
  return <Badge variant="destructive">{depth} queued</Badge>
}

function WorkerCard({ worker }: { worker: WorkerInfo }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg">
            SmolLM2-{worker.model.toUpperCase()}
          </CardTitle>
          <QueueBadge depth={worker.queue_depth} />
        </div>
        <p className="text-xs text-muted-foreground font-mono">{worker.stream}</p>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <p className="text-muted-foreground">Queue depth</p>
            <p className="font-semibold text-xl">{worker.queue_depth}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Pending ACK</p>
            <p className="font-semibold text-xl">{worker.pending}</p>
          </div>
        </div>

        <Separator />

        <div className="text-sm">
          <p className="text-muted-foreground mb-1">Last task</p>
          {worker.last_task ? (
            <div className="space-y-0.5">
              <p className="font-mono text-xs text-muted-foreground truncate">
                {worker.last_task.task_id}
              </p>
              <p className="truncate">{worker.last_task.message}</p>
            </div>
          ) : (
            <p className="text-muted-foreground italic">No tasks yet</p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function WorkerStatus() {
  const [workers, setWorkers]   = useState<WorkerInfo[]>([])
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [error, setError]       = useState<string | null>(null)

  useEffect(() => {
    async function fetchStatus() {
      try {
        const res  = await fetch(`${API_URL}/workers/status`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        setWorkers(data.workers)
        setLastUpdate(new Date())
        setError(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to fetch")
      }
    }

    fetchStatus()
    const id = setInterval(fetchStatus, POLL_INTERVAL)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-muted-foreground">
          Polling every {POLL_INTERVAL / 1000}s
        </h2>
        {lastUpdate && (
          <p className="text-xs text-muted-foreground">
            Last update: {lastUpdate.toLocaleTimeString()}
          </p>
        )}
      </div>

      {error && (
        <div className="rounded-md bg-destructive/10 text-destructive px-4 py-2 text-sm">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {workers.map(w => (
          <WorkerCard key={w.model} worker={w} />
        ))}
      </div>
    </div>
  )
}