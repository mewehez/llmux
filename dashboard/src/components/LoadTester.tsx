import type React from "react"
import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Lightning } from "@phosphor-icons/react"
import { ModelSelect } from "@/components/ModelSelect"
import { RunCard } from "@/components/RunCard"
import type { LoadTestSummary } from "@/components/MetricsPanel"
import { randomSampleMessage, simulateLoadTask } from "@/lib/mock"
import { API_URL, MOCK, postChat } from "@/lib/api"
import type { EventItem, LoadTestRun, ModelSelection, WorkerMetrics } from "@/lib/types"

type TaskPatch = Partial<LoadTestRun["tasks"][number]>

export function LoadTester({
  workers,
  onThroughput,
  onEvent,
  onComplete,
  runs,
  setRuns,
  running,
  setRunning,
}: {
  workers: WorkerMetrics[]
  onThroughput: (tokensPerSec: number) => void
  onEvent: (event: EventItem) => void
  onComplete: (summary: LoadTestSummary) => void
  // Lifted to App so results survive tab switches (base-ui remounts panels).
  runs: LoadTestRun[]
  setRuns: React.Dispatch<React.SetStateAction<LoadTestRun[]>>
  running: boolean
  setRunning: React.Dispatch<React.SetStateAction<boolean>>
}) {
  const [count, setCount] = useState(5)
  const [model, setModel] = useState<ModelSelection>("auto")
  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Close the run's event stream if the panel unmounts mid-run.
  useEffect(() => () => {
    esRef.current?.close()
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])

  function runTest() {
    if (running) return
    setRunning(true)

    const runId = `run-${Date.now()}`
    const run: LoadTestRun = {
      id: runId,
      time: new Date().toLocaleTimeString(),
      model,
      tasks: Array.from({ length: count }, (_, i) => ({
        id: `task-${i}`, prompt: "", response: "", status: "pending",
        tokens: 0, elapsedMs: null, tokensPerSec: null,
      })),
    }
    setRuns(prev => [run, ...prev].slice(0, 10))
    onEvent({ id: `ev-${Date.now()}`, time: new Date().toLocaleTimeString(), type: "loadtest", message: `Load test started — ${count}x ${model === "auto" ? "auto" : `SmolLM2-${model}`}` })

    function updateTask(taskId: string, patch: TaskPatch) {
      setRuns(prev => prev.map(r => r.id !== runId ? r : {
        ...r,
        tasks: r.tasks.map(t => t.id === taskId ? { ...t, ...patch } : t),
      }))
    }

    const done = new Set<string>()   // local task ids already finalized
    let finished = 0
    let errors = 0
    const elapsedTimes: number[] = []

    function cleanup() {
      esRef.current?.close()
      esRef.current = null
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    }

    function finishTask(taskId: string, p: {
      status: "done" | "error" | "timeout"; workerId: string; llmPod: string
      tokens: number; elapsedMs: number; tokensPerSec: number; response?: string
    }) {
      if (done.has(taskId)) return
      done.add(taskId)
      updateTask(taskId, {
        status: p.status, workerId: p.workerId, llmPod: p.llmPod,
        tokens: p.tokens, elapsedMs: p.elapsedMs, tokensPerSec: p.tokensPerSec,
        response: p.response ?? "",
      })
      if (p.status === "done") onThroughput(p.tokensPerSec)
      if (p.status === "error") errors += 1
      elapsedTimes.push(p.elapsedMs)
      finished += 1

      if (finished >= run.tasks.length) {
        cleanup()
        setRunning(false)
        const avg = elapsedTimes.length ? elapsedTimes.reduce((a, b) => a + b, 0) / elapsedTimes.length : 0
        onComplete({ count, model, done: finished - errors, errors, avgElapsed: avg })
        onEvent({ id: `ev-${Date.now()}-lt`, time: new Date().toLocaleTimeString(), type: "loadtest", message: `Load test finished — ${finished - errors}/${finished} ok, avg ${(avg / 1000).toFixed(1)}s` })
      }
    }

    // ── Mock mode: simulate locally (no network/connections) ──
    if (MOCK) {
      run.tasks.forEach(task => {
        const prompt = randomSampleMessage()
        updateTask(task.id, { status: "streaming", prompt })
        simulateLoadTask(model, workers, () => {}, ({ response, status, workerId, llmPod, tokens, elapsedMs }) =>
          finishTask(task.id, { status, workerId, llmPod, tokens, elapsedMs, response,
            tokensPerSec: elapsedMs > 0 ? tokens / (elapsedMs / 1000) : 0 }))
      })
      return
    }

    // ── Live mode: fire N requests, then drive completions off ONE events stream ──
    // (one SSE per run, not per task — avoids the browser's ~6-connection-per-host
    //  limit starving the dashboard's polling while a load test runs.)
    const idToLocal = new Map<string, string>()  // server task_id → local task id
    const startedAt = Date.now()

    run.tasks.forEach(task => {
      const prompt = randomSampleMessage()
      updateTask(task.id, { status: "streaming", prompt })
      postChat(prompt, model)
        .then(({ task_id }) => { idToLocal.set(task_id, task.id) })
        .catch(() => finishTask(task.id, { status: "error", workerId: "—", llmPod: "—", tokens: 0, elapsedMs: Date.now() - startedAt, tokensPerSec: 0 }))
    })

    const es = new EventSource(`${API_URL}/events/stream`)
    esRef.current = es
    es.onmessage = (ev) => {
      let e: Record<string, string>
      try { e = JSON.parse(ev.data) } catch { return }
      if (e.type !== "task_completed" && e.type !== "task_error") return
      const localId = idToLocal.get(e.task_id)
      if (!localId) return
      finishTask(localId, {
        status: e.type === "task_completed" ? "done" : "error",
        workerId: e.worker_id ?? "", llmPod: e.llm ?? "",
        tokens: Number(e.tokens) || 0,
        elapsedMs: Number(e.latency_ms) || (Date.now() - startedAt),
        tokensPerSec: Number(e.tok_s) || 0,
      })
    }
    // EventSource auto-reconnects on transient errors; keep it open.

    // Safety net: mark stragglers as timeout so a run always completes.
    timerRef.current = setTimeout(() => {
      run.tasks.forEach(task => finishTask(task.id, { status: "timeout", workerId: "—", llmPod: "—", tokens: 0, elapsedMs: Date.now() - startedAt, tokensPerSec: 0 }))
    }, 180000)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label>Concurrent requests</Label>
          <Input
            type="number"
            min={1}
            max={200}
            value={count}
            onChange={e => setCount(Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
            className="w-24"
            disabled={running}
          />
        </div>
        <div className="space-y-1.5">
          <Label>Model</Label>
          {/* wrap so the parent's space-y doesn't add a stray margin to base-ui
              Select's hidden form input (which otherwise mis-aligns the labels) */}
          <div>
            <ModelSelect value={model} onChange={setModel} includeAuto disabled={running} />
          </div>
        </div>
        <Button onClick={runTest} disabled={running}>{running ? "Running..." : "Run test"}</Button>
        {runs.length > 0 && !running && (
          <Button variant="outline" onClick={() => setRuns([])}>Clear history</Button>
        )}
      </div>

      {runs.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-8 text-center text-sm italic text-muted-foreground">
            <Lightning className="size-6 not-italic" />
            <p className="max-w-sm">
              Set the number of concurrent requests and hit Run test. Each run is logged below with
              per-request duration and throughput (completions stream over a single connection).
            </p>
          </CardContent>
        </Card>
      )}

      <div className="space-y-4">
        {runs.map(run => <RunCard key={run.id} run={run} />)}
      </div>
    </div>
  )
}
