// Model ids are data-driven (sourced from the API /config registry), so this is
// an open string alias rather than a closed union — adding a model is a config
// edit, not a type change. Known ids today: "135m", "360m".
export type Model = string

export type ModelSelection = Model | "auto"

export type WorkerState = "healthy" | "busy" | "draining" | "down" | "starting"

export interface WorkerMetrics {
  workerId:    string
  model:       Model
  status:      WorkerState
  healthy:     boolean
  loadScore:   number   // 0..1
  activeSlots: number
  maxSlots:    number
  queueDepth:  number
  latencyP50:  number   // ms
  totalReqs:   number
  errors:      number
  // Phase 2 live fields (from /replicas/status). Optional so mock data still fits.
  inflight?:      number
  liveTokS?:      number
  ttftMs?:        number
  lastLatencyMs?: number
  runner?:        string
}

export type EventType =
  | "scale_up"
  | "scale_down"
  | "request"
  | "complete"
  | "error"
  | "loadtest"
  | "system"

export interface EventItem {
  id:      string
  time:    string
  type:    EventType
  message: string
}

export type LoadTaskStatus = "pending" | "streaming" | "done" | "error" | "timeout"

export interface LoadTestTask {
  id:           string
  prompt:       string
  response:     string
  status:       LoadTaskStatus
  workerId?:    string
  llmPod?:      string
  tokens:       number
  elapsedMs:    number | null
  tokensPerSec: number | null
}

export interface LoadTestRun {
  id:    string
  time:  string
  model: ModelSelection
  tasks: LoadTestTask[]
}

export interface ChatMessage {
  role:    "user" | "assistant"
  content: string
  model?:  ModelSelection
  workerId?: string
  llmPod?: string
  tokens?: number
  elapsedMs?: number
}
