import { useEffect, useState } from "react"
import { API_URL } from "./api"

export interface SeriesPoint {
  t:          number
  tokens:     number
  requests:   number
  latencyP50: number
}

export interface ModelTimeseries {
  model:       string
  window:      number
  count:       number
  requestRate: number
  latency:     { p50: number; p95: number; p99: number; avg: number }
  tokSec:      { avg: number; p50: number }
  series:      SeriesPoint[]
  queueDepth:  number
  activeSlots: number
  maxSlots:    number
}

/** Polls the API for real, server-computed time-series + percentiles for a model. */
export function useTimeseries(
  model: string | null,
  intervalMs = 2000,
): { data: ModelTimeseries | null; isLive: boolean } {
  const [data, setData] = useState<ModelTimeseries | null>(null)
  const [isLive, setLive] = useState(false)

  useEffect(() => {
    if (!model) return
    let cancelled = false

    async function poll() {
      try {
        const res = await fetch(`${API_URL}/metrics/timeseries?model=${model}`)
        if (!res.ok) throw new Error(`timeseries: ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setData(json.models?.[0] ?? null)
          setLive(true)
        }
      } catch {
        if (!cancelled) setLive(false)
      }
    }

    poll()
    const id = setInterval(poll, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [model, intervalMs])

  return { data, isLive }
}
