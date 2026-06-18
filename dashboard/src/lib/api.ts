import { useEffect, useState } from "react"
import type { Model, ModelSelection, WorkerMetrics } from "./types"

export const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

// Mock data is opt-in only (VITE_MOCK=1). A monitoring tool must never silently
// invent numbers when the API is down — see useCluster's "offline" mode.
export const MOCK = import.meta.env.VITE_MOCK === "1"

interface ReplicaStatus {
  workerId:      string
  model:         Model
  totalReqs:     number
  errors:        number
  latencyP50:    number
  lastLatencyMs: number
  tokensPerSec:  number
  state:         string
  inflight:      number
  liveTokS:      number
  ttftMs:        number
  lastSeen:      number
  healthy:       boolean
}

interface WorkerStreamStatus {
  model:       Model
  stream:      string
  queue_depth: number
  pending:     number
  last_task:   { task_id: string; message: string } | null
  llm_url:     string
}

export interface SlotPod {
  pod:        string   // full llama.cpp pod name
  processing: number   // slots in flight
  total:      number   // = llama.cpp --parallel (max concurrent requests)
  deferred:   number   // requests queued inside llama.cpp waiting for a slot
}

interface ClusterModelStatus {
  model:       Model
  replicas:    number
  maxReplicas: number
  llmPods:     number
  maxLlmPods:  number
  queueDepth:  number
  activeSlots: number
  maxSlots:    number
  deferred:    number
  slotPods:    SlotPod[]
}

export interface ModelSlots {
  activeSlots: number
  maxSlots:    number
  deferred:    number
  maxLlmPods:  number
  pods:        SlotPod[]
}

export async function fetchWorkersStatus(): Promise<WorkerStreamStatus[]> {
  const res = await fetch(`${API_URL}/workers/status`)
  if (!res.ok) throw new Error(`workers/status: ${res.status}`)
  const data = await res.json()
  return data.workers
}

export async function fetchReplicasStatus(): Promise<ReplicaStatus[]> {
  const res = await fetch(`${API_URL}/replicas/status`)
  if (!res.ok) throw new Error(`replicas/status: ${res.status}`)
  const data = await res.json()
  return data.replicas
}

export async function fetchClusterStatus(): Promise<{ models: ClusterModelStatus[]; scaleEvents: number }> {
  const res = await fetch(`${API_URL}/cluster/status`)
  if (!res.ok) throw new Error(`cluster/status: ${res.status}`)
  const data = await res.json()
  return { models: data.models ?? [], scaleEvents: data.scaleEvents ?? 0 }
}

/**
 * Combines /replicas/status, /workers/status and /cluster/status into the
 * WorkerMetrics[] shape the dashboard components expect.
 */
export async function fetchCluster(): Promise<{
  workers: WorkerMetrics[]
  redisQueues: Record<Model, number>
  modelSlots: Record<Model, ModelSlots>
  scaleEvents: number
}> {
  const [replicas, workerStreams, cluster] = await Promise.all([
    fetchReplicasStatus(),
    fetchWorkersStatus(),
    fetchClusterStatus(),
  ])
  const clusterModels = cluster.models

  const redisQueues = {} as Record<Model, number>
  for (const w of workerStreams) {
    redisQueues[w.model] = w.queue_depth
  }

  const modelSlots = {} as Record<Model, ModelSlots>
  for (const m of clusterModels) {
    modelSlots[m.model] = {
      activeSlots: m.activeSlots,
      maxSlots:    m.maxSlots,
      deferred:    m.deferred,
      maxLlmPods:  m.maxLlmPods,
      pods:        m.slotPods ?? [],
    }
  }

  const workers: WorkerMetrics[] = replicas.map(r => ({
    workerId:      r.workerId,
    model:         r.model,
    status:        !r.healthy ? "down" : (r.state === "busy" ? "busy" : "healthy"),
    healthy:       r.healthy,
    loadScore:     0,
    activeSlots:   r.inflight ?? 0,
    maxSlots:      1,
    queueDepth:    redisQueues[r.model] ?? 0,
    latencyP50:    r.latencyP50,
    totalReqs:     r.totalReqs,
    errors:        r.errors,
    inflight:      r.inflight,
    liveTokS:      r.liveTokS,
    ttftMs:        r.ttftMs,
    lastLatencyMs: r.lastLatencyMs,
  }))

  return { workers, redisQueues, modelSlots, scaleEvents: cluster.scaleEvents }
}

/**
 * Polls the real API for cluster state. Falls back to the provided mock
 * workers/redisQueues if the API is unreachable.
 */
export type ClusterMode = "live" | "mock" | "offline"

export function useCluster(
  mockWorkers: WorkerMetrics[],
  mockRedisQueues: Record<Model, number>,
  mockModelSlots: Record<Model, ModelSlots>,
  intervalMs = 1000,
): {
  workers: WorkerMetrics[]
  redisQueues: Record<Model, number>
  modelSlots: Record<Model, ModelSlots>
  scaleEvents: number
  isLive: boolean
  mode: ClusterMode
} {
  const [live, setLive] = useState<Awaited<ReturnType<typeof fetchCluster>> | null>(null)
  const [reachable, setReachable] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const data = await fetchCluster()
        if (!cancelled) { setLive(data); setReachable(true) }
      } catch {
        if (!cancelled) { setLive(null); setReachable(false) }
      }
    }

    poll()
    const id = setInterval(poll, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [intervalMs])

  if (reachable && live) return { ...live, isLive: true, mode: "live" }
  // API unreachable: use mock ONLY if explicitly enabled; otherwise be honest.
  if (MOCK) {
    return { workers: mockWorkers, redisQueues: mockRedisQueues, modelSlots: mockModelSlots, scaleEvents: 0, isLive: false, mode: "mock" }
  }
  return {
    workers: [],
    redisQueues: {} as Record<Model, number>,
    modelSlots: {} as Record<Model, ModelSlots>,
    scaleEvents: 0,
    isLive: false,
    mode: "offline",
  }
}

interface ChatCreated {
  task_id:    string
  session_id: string
  model:      Model
  stream:     string
}

export async function postChat(message: string, model: ModelSelection, sessionId?: string): Promise<ChatCreated> {
  const res = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, model, session_id: sessionId }),
  })
  if (!res.ok) throw new Error(`chat: ${res.status}`)
  return res.json()
}

/**
 * Opens an SSE connection for a chat task, streaming tokens as they arrive.
 * Mirrors the signature of lib/mock's simulate* functions so it's a drop-in
 * swap from the caller's perspective. Returns a cancel function.
 */
export function streamChat(
  taskId: string,
  resolvedModel: Model,
  onToken: (text: string) => void,
  onDone: (info: { workerId: string; llmPod: string; model: Model; tokens: number; elapsedMs: number }) => void,
  onError: (message: string) => void,
): () => void {
  const start = Date.now()
  let tokens = 0
  const source = new EventSource(`${API_URL}/sse/${taskId}`)

  source.onmessage = (ev) => {
    const payload = JSON.parse(ev.data)
    if (payload.type === "token") {
      tokens += 1
      onToken(payload.content)
    } else if (payload.type === "done") {
      onDone({
        workerId: payload.worker_id ?? `worker-${resolvedModel}`,
        llmPod:   payload.llm ?? `llm-${resolvedModel}`,
        model:    (payload.model as Model) ?? resolvedModel,
        tokens,
        elapsedMs: Date.now() - start,
      })
      source.close()
    } else if (payload.type === "error") {
      onError(payload.content ?? "stream error")
      source.close()
    }
  }

  source.onerror = () => {
    onError("connection lost")
    source.close()
  }

  return () => source.close()
}
