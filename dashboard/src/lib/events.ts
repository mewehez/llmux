import { useEffect, useState } from "react"
import { API_URL } from "./api"

/** A real lifecycle event from the API's llm:events stream (via /sse/events). */
export interface LiveEvent {
  id:         string
  type:       string   // task_started|task_completed|task_error|worker_up|worker_down|scale_up|scale_down
  ts:         string   // unix ms (as a string, from Redis)
  model?:     string
  worker_id?: string
  task_id?:   string
  runner?:    string
  llm?:       string
  tokens?:    string
  tok_s?:     string
  ttft_ms?:   string
  latency_ms?: string
  error?:     string
  reason?:    string   // task_dead
  attempt?:   string   // task_error
  component?: string   // scale events: "workers" | "llmPods"
  from?:      string
  to?:        string
}

/**
 * Subscribes to the real server-sent activity feed. Returns the most recent
 * events (newest first) and whether the stream is currently connected. No mock
 * fallback — if the API is down, isLive is false and the list is what we last saw.
 */
export function useEvents(limit = 100): { events: LiveEvent[]; isLive: boolean } {
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [isLive, setLive] = useState(false)

  useEffect(() => {
    const source = new EventSource(`${API_URL}/events/stream`)
    source.onopen = () => setLive(true)
    source.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data) as LiveEvent
        setLive(true)
        // Dedupe by id: SSE replay + reconnects (and StrictMode double-mounts in
        // dev) can deliver the same event twice.
        setEvents(prev => (prev.some(p => p.id === ev.id) ? prev : [ev, ...prev].slice(0, limit)))
      } catch {
        /* ignore malformed frames */
      }
    }
    source.onerror = () => setLive(false)
    return () => source.close()
  }, [limit])

  return { events, isLive }
}
