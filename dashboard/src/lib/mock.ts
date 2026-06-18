import { useEffect, useState } from "react"
import type { ChatMessage, EventItem, EventType, Model, ModelSelection, WorkerMetrics } from "./types"

function pickWorker(model: ModelSelection, workers: WorkerMetrics[]): WorkerMetrics {
  if (model === "auto") {
    return workers[Math.floor(Math.random() * workers.length)] ?? workers[0]
  }
  return workers.find(w => w.model === model) ?? workers[0]
}

/**
 * Self-contained mock data layer. Mirrors the shape we expect from the real
 * API (/workers/status, /chat + /sse/{id}) so it can be swapped out later
 * without touching component code.
 */

const INITIAL_WORKERS: WorkerMetrics[] = [
  {
    workerId: "worker-135m-1", model: "135m", status: "healthy", healthy: true,
    loadScore: 0.12, activeSlots: 0, maxSlots: 4, queueDepth: 0,
    latencyP50: 180, totalReqs: 42, errors: 0,
  },
  {
    workerId: "worker-360m-1", model: "360m", status: "healthy", healthy: true,
    loadScore: 0.28, activeSlots: 1, maxSlots: 4, queueDepth: 0,
    latencyP50: 540, totalReqs: 31, errors: 0,
  },
]

export const MAX_REPLICAS: Record<Model, number> = {
  "135m": 3,
  "360m": 2,
}

const INITIAL_REDIS_QUEUES: Record<Model, number> = {
  "135m": 3,
  "360m": 9,
}

const SAMPLE_MESSAGES = [
  "Count from 1 to 10",
  "What is the capital of France?",
  "Write a haiku about rain",
  "List 3 colors",
  "Say hello",
  "Explain Kubernetes in one sentence",
]

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}

function randomWalk(value: number, magnitude: number, lo: number, hi: number) {
  return clamp(value + (Math.random() - 0.5) * magnitude, lo, hi)
}

let eventSeq = 0
export function makeEvent(type: EventType, message: string): EventItem {
  eventSeq += 1
  return {
    id: `ev-${Date.now()}-${eventSeq}`,
    time: new Date().toLocaleTimeString(),
    type,
    message,
  }
}

/**
 * Drives a small simulated cluster: workers drift between idle/busy,
 * occasionally queue up, and emit scale events when queue depth spikes.
 */
export function useMockCluster(tickMs = 1200) {
  const [workers, setWorkers] = useState<WorkerMetrics[]>(INITIAL_WORKERS)
  const [redisQueues, setRedisQueues] = useState<Record<Model, number>>(INITIAL_REDIS_QUEUES)
  const [events, setEvents]   = useState<EventItem[]>([
    makeEvent("system", "Dashboard connected (mock data)"),
  ])

  const pushEvent = (e: EventItem) =>
    setEvents(prev => [e, ...prev].slice(0, 50))

  useEffect(() => {
    const id = setInterval(() => {
      setWorkers(prev => prev.map(w => {
        const queueDepth = Math.max(0, Math.round(randomWalk(w.queueDepth, 1.4, 0, 8)))
        const activeSlots = clamp(
          Math.round(randomWalk(w.activeSlots, 1.2, 0, w.maxSlots)),
          0, w.maxSlots,
        )
        const loadScore = clamp(randomWalk(w.loadScore, 0.15, 0.02, 0.98), 0, 1)
        const latencyP50 = Math.round(randomWalk(w.latencyP50, 80, 80, 3000))
        const status: WorkerMetrics["status"] =
          queueDepth >= 2 ? "busy" : activeSlots > 0 ? "busy" : "healthy"

        if (queueDepth >= 2 && w.queueDepth < 2) {
          pushEvent(makeEvent("scale_up", `${w.workerId}: queue depth ${queueDepth} → scaling up`))
        }
        if (queueDepth === 0 && w.queueDepth >= 2) {
          pushEvent(makeEvent("scale_down", `${w.workerId}: queue drained → scaling down`))
        }

        return {
          ...w,
          queueDepth,
          activeSlots,
          loadScore,
          latencyP50,
          status,
          healthy: true,
        }
      }))
    }, tickMs)

    return () => clearInterval(id)
  }, [tickMs])

  useEffect(() => {
    const id = setInterval(() => {
      setRedisQueues(prev => ({
        "135m": Math.max(0, Math.round(randomWalk(prev["135m"], 2.5, 0, 30))),
        "360m": Math.max(0, Math.round(randomWalk(prev["360m"], 2.5, 0, 30))),
      }))
    }, tickMs)

    return () => clearInterval(id)
  }, [tickMs])

  const clearEvents = () => setEvents([])

  return { workers, redisQueues, events, pushEvent, clearEvents }
}

/**
 * Simulates a streaming chat response token-by-token, calling back like an
 * SSE stream would. Returns a cancel function.
 */
export function simulateChatStream(
  model: ModelSelection,
  workers: WorkerMetrics[],
  onToken: (text: string) => void,
  onDone: (info: { workerId: string; llmPod: string; model: Model; tokens: number; elapsedMs: number }) => void,
): () => void {
  const worker = pickWorker(model, workers)
  const response = MOCK_RESPONSES[Math.floor(Math.random() * MOCK_RESPONSES.length)]
  const tokens = response.split(/(\s+)/).filter(Boolean)

  const start = Date.now()
  let i = 0
  let cancelled = false

  const tick = () => {
    if (cancelled) return
    if (i < tokens.length) {
      onToken(tokens[i])
      i += 1
      setTimeout(tick, worker.model === "360m" ? 90 : 45)
    } else {
      onDone({ workerId: worker.workerId, llmPod: `llm-${worker.model}`, model: worker.model, tokens: tokens.length, elapsedMs: Date.now() - start })
    }
  }
  setTimeout(tick, 200)

  return () => { cancelled = true }
}

/**
 * Simulates one load-test request: streams tokens like simulateChatStream,
 * but also returns the prompt and may end in "error" or "timeout" so the
 * load tester can show realistic outcomes.
 */
export function simulateLoadTask(
  model: ModelSelection,
  workers: WorkerMetrics[],
  onToken: (text: string) => void,
  onDone: (result: {
    prompt: string
    response: string
    status: "done" | "error" | "timeout"
    workerId: string
    llmPod: string
    model: Model
    tokens: number
    elapsedMs: number
  }) => void,
): () => void {
  const worker = pickWorker(model, workers)
  const idx = Math.floor(Math.random() * SAMPLE_MESSAGES.length)
  const prompt = SAMPLE_MESSAGES[idx]
  const fullResponse = MOCK_RESPONSES[idx]
  const tokens = fullResponse.split(/(\s+)/).filter(Boolean)

  const roll = Math.random()
  const willError = roll < 0.08
  const willTimeout = !willError && roll < 0.16
  const perTok = worker.model === "360m" ? 90 : 45

  const start = Date.now()
  let i = 0
  let cancelled = false

  const tick = () => {
    if (cancelled) return
    if (willTimeout && Date.now() - start > 4000) {
      onDone({ prompt, response: tokens.slice(0, i).join(""), status: "timeout", workerId: worker.workerId, llmPod: `llm-${worker.model}`, model: worker.model, tokens: i, elapsedMs: Date.now() - start })
      return
    }
    if (willError && i === Math.floor(tokens.length / 2)) {
      onDone({ prompt, response: tokens.slice(0, i).join(""), status: "error", workerId: worker.workerId, llmPod: `llm-${worker.model}`, model: worker.model, tokens: i, elapsedMs: Date.now() - start })
      return
    }
    if (i < tokens.length) {
      onToken(tokens[i])
      i += 1
      setTimeout(tick, perTok)
    } else {
      onDone({ prompt, response: fullResponse, status: "done", workerId: worker.workerId, llmPod: `llm-${worker.model}`, model: worker.model, tokens: tokens.length, elapsedMs: Date.now() - start })
    }
  }
  setTimeout(tick, 150)

  return () => { cancelled = true }
}

const MOCK_RESPONSES = [
  "Sure! 1, 2, 3, 4, 5, 6, 7, 8, 9, 10. That's counting from one to ten.",
  "The capital of France is Paris, a city known for its art, culture, and the Eiffel Tower.",
  "Rain falls soft and slow / Whispers on the window pane / Earth drinks deep again.",
  "Here are three colors: red, blue, and green.",
  "Hello! How can I help you today?",
  "Kubernetes is a container orchestration platform that automates deployment, scaling, and management of containerized applications.",
]

export function randomSampleMessage() {
  return SAMPLE_MESSAGES[Math.floor(Math.random() * SAMPLE_MESSAGES.length)]
}

/** Hook that keeps a rolling history of throughput samples (tokens/sec). */
export function useThroughputHistory(limit = 20) {
  const [history, setHistory] = useState<number[]>([])
  const push = (value: number) =>
    setHistory(prev => [...prev, value].slice(-limit))
  return { history, push }
}

export type { ChatMessage, EventItem, EventType, Model, WorkerMetrics }
