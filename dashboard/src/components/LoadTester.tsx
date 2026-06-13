import { useState, useRef } from "react"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

type Model = "135m" | "360m"
type TaskStatus = "pending" | "streaming" | "done" | "error"

interface Task {
  id:        string
  taskId:    string
  model:     Model
  status:    TaskStatus
  tokens:    number
  elapsed:   number | null   // ms from send to done
  startedAt: number
}

const PRESET_MESSAGES = [
  "Count from 1 to 10",
  "What is the capital of France?",
  "Write a haiku about rain",
  "List 3 colors",
  "Say hello",
]

function TaskRow({ task }: { task: Task }) {
  const statusColors: Record<TaskStatus, string> = {
    pending:   "border-slate-400 text-slate-500",
    streaming: "border-yellow-500 text-yellow-600",
    done:      "border-green-500 text-green-600",
    error:     "border-red-500 text-red-600",
  }

  return (
    <div className="grid grid-cols-5 gap-2 text-sm py-1.5 items-center border-b last:border-0">
      <span className="font-mono text-xs text-muted-foreground truncate">
        {task.taskId.slice(0, 8)}
      </span>
      <span className="text-xs text-muted-foreground">
        SmolLM2-{task.model.toUpperCase()}
      </span>
      <Badge variant="outline" className={statusColors[task.status]}>
        {task.status}
      </Badge>
      <span className="text-right tabular-nums">
        {task.tokens > 0 ? `${task.tokens} tok` : "—"}
      </span>
      <span className="text-right tabular-nums text-muted-foreground">
        {task.elapsed != null
          ? `${(task.elapsed / 1000).toFixed(1)}s`
          : task.status === "streaming"
          ? `${((Date.now() - task.startedAt) / 1000).toFixed(1)}s`
          : "—"}
      </span>
    </div>
  )
}

export function LoadTester() {
  const [count, setCount]     = useState(5)
  const [model, setModel]     = useState<Model>("135m")
  const [running, setRunning] = useState(false)
  const [tasks, setTasks]     = useState<Task[]>([])
  const esRefs                = useRef<Map<string, EventSource>>(new Map())

  const updateTask = (id: string, patch: Partial<Task>) =>
    setTasks(prev => prev.map(t => t.id === id ? { ...t, ...patch } : t))

  async function runTest() {
    if (running) return

    // Close any lingering SSE connections
    esRefs.current.forEach(es => es.close())
    esRefs.current.clear()

    setRunning(true)
    setTasks([])

    const message = PRESET_MESSAGES[Math.floor(Math.random() * PRESET_MESSAGES.length)]

    // Fire all requests concurrently
    const promises = Array.from({ length: count }, async (_, i) => {
      const id = `task-${i}`

      try {
        const res = await fetch(`${API_URL}/chat`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify({ message, model }),
        })

        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const { task_id } = await res.json()

        const task: Task = {
          id,
          taskId:    task_id,
          model,
          status:    "pending",
          tokens:    0,
          elapsed:   null,
          startedAt: Date.now(),
        }

        setTasks(prev => [...prev, task])

        // Open SSE for each task
        await new Promise<void>((resolve) => {
          const es = new EventSource(`${API_URL}/sse/${task_id}`)
          esRefs.current.set(id, es)

          es.onmessage = (event) => {
            const payload = JSON.parse(event.data)

            if (payload.type === "token") {
              updateTask(id, {
                status: "streaming",
                tokens: (tasks.find(t => t.id === id)?.tokens ?? 0) + 1,
              })
              // Use functional update to get latest token count
              setTasks(prev => prev.map(t =>
                t.id === id ? { ...t, status: "streaming", tokens: t.tokens + 1 } : t
              ))
            }

            if (payload.type === "done") {
              setTasks(prev => prev.map(t =>
                t.id === id
                  ? { ...t, status: "done", elapsed: Date.now() - t.startedAt }
                  : t
              ))
              es.close()
              esRefs.current.delete(id)
              resolve()
            }

            if (payload.type === "error") {
              setTasks(prev => prev.map(t =>
                t.id === id ? { ...t, status: "error" } : t
              ))
              es.close()
              esRefs.current.delete(id)
              resolve()
            }
          }

          es.onerror = () => {
            setTasks(prev => prev.map(t =>
              t.id === id ? { ...t, status: "error" } : t
            ))
            es.close()
            esRefs.current.delete(id)
            resolve()
          }
        })

      } catch {
        setTasks(prev => [...prev, {
          id,
          taskId:    "failed",
          model,
          status:    "error",
          tokens:    0,
          elapsed:   null,
          startedAt: Date.now(),
        }])
      }
    })

    await Promise.all(promises)
    setRunning(false)
  }

  const done      = tasks.filter(t => t.status === "done").length
  const streaming = tasks.filter(t => t.status === "streaming").length
  const avgElapsed = tasks
    .filter(t => t.elapsed != null)
    .map(t => t.elapsed!)
    .reduce((a, b, _, arr) => a + b / arr.length, 0)

  return (
    <div className="space-y-4">

      {/* Controls */}
      <div className="flex flex-wrap gap-4 items-end">
        <div className="space-y-1.5">
          <Label>Concurrent requests</Label>
          <Input
            type="number"
            min={1}
            max={20}
            value={count}
            onChange={e => setCount(Number(e.target.value))}
            className="w-24"
            disabled={running}
          />
        </div>

        <div className="space-y-1.5">
          <Label>Model</Label>
          <div className="flex gap-2">
            {(["135m", "360m"] as Model[]).map(m => (
              <button
                key={m}
                onClick={() => setModel(m)}
                disabled={running}
                className={[
                  "px-3 py-1.5 rounded-md text-sm font-medium border transition-colors",
                  model === m
                    ? "bg-primary text-primary-foreground border-primary"
                    : "bg-background text-muted-foreground border-border hover:border-primary",
                  running ? "opacity-50 cursor-not-allowed" : "",
                ].join(" ")}
              >
                SmolLM2-{m.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <Button onClick={runTest} disabled={running} className="mb-0.5">
          {running ? "Running..." : "Run test"}
        </Button>

        {tasks.length > 0 && !running && (
          <Button
            variant="ghost"
            onClick={() => setTasks([])}
            className="mb-0.5"
          >
            Clear
          </Button>
        )}
      </div>

      <Separator />

      {/* Summary stats */}
      {tasks.length > 0 && (
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: "Total",     value: tasks.length },
            { label: "Streaming", value: streaming,
              className: streaming > 0 ? "text-yellow-600" : "" },
            { label: "Done",      value: done,
              className: done > 0 ? "text-green-600" : "" },
            { label: "Avg time",
              value: done > 0 ? `${(avgElapsed / 1000).toFixed(1)}s` : "—" },
          ].map(stat => (
            <Card key={stat.label}>
              <CardContent className="pt-4 pb-3">
                <p className="text-xs text-muted-foreground">{stat.label}</p>
                <p className={`text-2xl font-bold tabular-nums ${stat.className ?? ""}`}>
                  {stat.value}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Task table */}
      {tasks.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <div className="grid grid-cols-5 gap-2 text-xs text-muted-foreground font-medium">
              <span>Task ID</span>
              <span>Model</span>
              <span>Status</span>
              <span className="text-right">Tokens</span>
              <span className="text-right">Elapsed</span>
            </div>
          </CardHeader>
          <CardContent className="pt-0 max-h-80 overflow-y-auto">
            {tasks.map(task => (
              <TaskRow key={task.id} task={task} />
            ))}
          </CardContent>
        </Card>
      )}

      {tasks.length === 0 && !running && (
        <p className="text-sm text-muted-foreground italic text-center py-8">
          Set the number of concurrent requests and hit Run test.
          Watch the Worker Status tab to see the queue build and drain.
        </p>
      )}
    </div>
  )
}