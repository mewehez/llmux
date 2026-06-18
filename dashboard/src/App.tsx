import { useEffect, useMemo, useState } from "react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { StatsBar } from "@/components/StatsBar"
import { ModelGroup } from "@/components/ModelGroup"
import { ActivityFeed } from "@/components/ActivityFeed"
import { ChatPanel } from "@/components/ChatPanel"
import { LoadTester } from "@/components/LoadTester"
import { BenchmarkPanel } from "@/components/BenchmarkPanel"
import { MetricsPanel, type LoadTestSummary } from "@/components/MetricsPanel"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useMockCluster, useThroughputHistory } from "@/lib/mock"
import { useCluster, type ModelSlots } from "@/lib/api"
import { useConfig } from "@/lib/config"
import type { ChatMessage, EventItem, LoadTestRun, Model } from "@/lib/types"

// Small localStorage helpers so chat history + load runs survive a page reload.
function loadLS<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback } catch { return fallback }
}
function saveLS(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* quota / disabled */ }
}

export default function App() {
  // Model set comes from the API registry (/config); falls back to defaults offline.
  const { models: modelInfos } = useConfig()
  const maxReplicasFor = (id: Model) => modelInfos.find(m => m.id === id)?.maxReplicas ?? 1

  // Mock cluster only feeds the UI when VITE_MOCK=1 (see useCluster).
  const { workers: mockWorkers, redisQueues: mockRedisQueues } = useMockCluster()
  const mockModelSlots = useMemo(() => {
    const acc = {} as Record<Model, ModelSlots>
    for (const info of modelInfos) {
      const m = info.id
      const ws = mockWorkers.filter(w => w.model === m)
      const activeSlots = ws.reduce((s, w) => s + w.activeSlots, 0)
      const maxSlots    = ws.reduce((s, w) => s + w.maxSlots, 0)
      acc[m] = {
        activeSlots, maxSlots,
        deferred: mockRedisQueues[m] ?? 0,
        maxLlmPods: info.maxLlmPods,
        pods: [{ pod: `llm-${m}-mock`, processing: activeSlots, total: maxSlots || 4, deferred: mockRedisQueues[m] ?? 0 }],
      }
    }
    return acc
  }, [mockWorkers, mockRedisQueues, modelInfos])

  // scaleEvents is a cumulative server-side counter (survives reloads).
  const { workers, redisQueues, modelSlots, mode, scaleEvents } = useCluster(mockWorkers, mockRedisQueues, mockModelSlots)
  const { push: pushThroughput } = useThroughputHistory()

  const [lastLoadTest, setLastLoadTest] = useState<LoadTestSummary | null>(null)
  const [selectedModel, setSelectedModel] = useState<Model>(modelInfos[0]?.id ?? "135m")

  // Chat/load-test state lives here so it survives tab switches AND page reloads
  // (persisted to localStorage; base-ui remounts inactive panels otherwise).
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(() => loadLS<ChatMessage[]>("llm.chat", []))
  const [loadRuns, setLoadRuns] = useState<LoadTestRun[]>(() => loadLS<LoadTestRun[]>("llm.runs", []))
  const [loadRunning, setLoadRunning] = useState(false)
  useEffect(() => saveLS("llm.chat", chatMessages), [chatMessages])
  useEffect(() => saveLS("llm.runs", loadRuns), [loadRuns])

  // Total queued = sum of DISTINCT model queue depths (not per-worker, which would multiply).
  const queued = Object.values(redisQueues).reduce((a, b) => a + b, 0)

  // The real activity feed is the SSE ActivityFeed; chat/load local events are unused.
  const noopEvent = (_e: EventItem) => {}

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b bg-background px-6 py-3">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-green-500" />
          <h1 className="font-mono text-sm font-semibold tracking-wide">LLM SERVER DASHBOARD</h1>
        </div>
      </header>

      <div className="space-y-6 p-6">
        <StatsBar workers={workers} queued={queued} scaleEvents={scaleEvents} />

        <Tabs defaultValue="overview">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
            <TabsTrigger value="chat">Chat</TabsTrigger>
            <TabsTrigger value="load">Load Tester</TabsTrigger>
            <TabsTrigger value="benchmark">Benchmark</TabsTrigger>
            <TabsTrigger value="metrics">Metrics</TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="space-y-6">
            {mode === "offline" && (
              <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                API unreachable — showing no data. Start the stack, or set <code className="font-mono">VITE_MOCK=1</code> for demo data.
              </div>
            )}
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">Model</span>
              <Select value={selectedModel} onValueChange={(v) => setSelectedModel(v as Model)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {modelInfos.map(m => (
                    <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <ModelGroup
              model={selectedModel}
              replicas={workers.filter(w => w.model === selectedModel)}
              maxReplicas={maxReplicasFor(selectedModel)}
              queueDepth={redisQueues[selectedModel] ?? 0}
              slots={modelSlots[selectedModel]}
            />
          </TabsContent>

          <TabsContent value="activity">
            <ActivityFeed />
          </TabsContent>

          <TabsContent value="chat">
            <ChatPanel workers={workers} onThroughput={pushThroughput} onEvent={noopEvent} messages={chatMessages} setMessages={setChatMessages} />
          </TabsContent>

          <TabsContent value="load">
            <LoadTester workers={workers} onThroughput={pushThroughput} onEvent={noopEvent} onComplete={setLastLoadTest} runs={loadRuns} setRuns={setLoadRuns} running={loadRunning} setRunning={setLoadRunning} />
          </TabsContent>

          <TabsContent value="benchmark">
            <BenchmarkPanel />
          </TabsContent>

          <TabsContent value="metrics">
            <MetricsPanel lastLoadTest={lastLoadTest} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
