import { useEffect, useState } from "react"
import { API_URL } from "./api"

export interface BenchmarkSpec {
  model:       string
  count:       number
  concurrency: number
  maxTokens?:  number
  runner?:     string
  llmUrl?:     string
  llmModel?:   string
}

/** Enqueue a benchmark run (POST /benchmark → llm:bench:work → benchmark-runner). */
export async function postBenchmark(spec: BenchmarkSpec): Promise<void> {
  const body: Record<string, unknown> = {
    model: spec.model, count: spec.count, concurrency: spec.concurrency,
  }
  if (spec.maxTokens) body.max_tokens = spec.maxTokens
  if (spec.runner)    body.runner = spec.runner
  if (spec.llmUrl)    body.llm_url = spec.llmUrl
  if (spec.llmModel)  body.llm_model = spec.llmModel
  const res = await fetch(`${API_URL}/benchmark`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`benchmark: ${res.status}`)
}

/** One benchmark run as written by worker/benchmark.py and served by /benchmark. */
export interface BenchRun {
  run_id:           string
  ts:               number
  model:            string
  runner:           string
  llm_model:        string
  count:            number
  completed:        number
  errors:           number
  concurrency:      number
  max_tokens:       number
  ttft_p50_ms:      number
  ttft_p95_ms:      number
  latency_p50_ms:   number
  latency_p95_ms:   number
  decode_tok_s_avg: number
  decode_tok_s_p50: number
  throughput_tok_s: number
  total_tokens:     number
  wall_s:           number
}

export function useBenchmarks(intervalMs = 5000): { runs: BenchRun[]; isLive: boolean } {
  const [runs, setRuns] = useState<BenchRun[]>([])
  const [isLive, setLive] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const res = await fetch(`${API_URL}/benchmark?limit=25`)
        if (!res.ok) throw new Error(`benchmark: ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setRuns(json.runs ?? [])
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
  }, [intervalMs])

  return { runs, isLive }
}
