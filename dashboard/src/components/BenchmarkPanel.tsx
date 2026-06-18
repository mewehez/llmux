import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ModelSelect } from "@/components/ModelSelect"
import { postBenchmark, useBenchmarks } from "@/lib/benchmark"
import type { ModelSelection } from "@/lib/types"

/** Runner benchmark comparison, backed by /benchmark (worker/benchmark.py). */
export function BenchmarkPanel() {
  const { runs, isLive } = useBenchmarks()
  const [model, setModel] = useState<ModelSelection>("135m")
  const [count, setCount] = useState(20)
  const [concurrency, setConcurrency] = useState(4)
  const [status, setStatus] = useState<"idle" | "queued" | "error">("idle")

  async function run() {
    setStatus("idle")
    try {
      await postBenchmark({ model: model === "auto" ? "135m" : model, count, concurrency })
      setStatus("queued")
      setTimeout(() => setStatus("idle"), 4000)
    } catch {
      setStatus("error")
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Run a benchmark</CardTitle>
          <Badge
            variant="outline"
            className={isLive ? "border-green-500 text-green-600" : "border-border text-muted-foreground"}
          >
            {isLive ? "live" : "offline"}
          </Badge>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <Label>Model</Label>
              <div><ModelSelect value={model} onChange={setModel} /></div>
            </div>
            <div className="space-y-1.5">
              <Label>Requests</Label>
              <Input type="number" min={1} max={500} value={count}
                onChange={e => setCount(Math.max(1, Math.min(500, Number(e.target.value) || 1)))} className="w-24" />
            </div>
            <div className="space-y-1.5">
              <Label>Concurrency</Label>
              <Input type="number" min={1} max={64} value={concurrency}
                onChange={e => setConcurrency(Math.max(1, Math.min(64, Number(e.target.value) || 1)))} className="w-24" />
            </div>
            <Button onClick={run}>Run benchmark</Button>
            {status === "queued" && <span className="text-xs text-green-600">Queued — results appear below when the runner finishes.</span>}
            {status === "error" && <span className="text-xs text-destructive">Failed to queue (is the API up?).</span>}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            To compare backends, run the worker CLI with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono">--runner ollama --llm-url …</code>.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Results</CardTitle></CardHeader>
        <CardContent>
          {runs.length === 0 ? (
            <p className="py-6 text-center text-sm italic text-muted-foreground">No benchmark runs yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>model</TableHead>
                  <TableHead>runner</TableHead>
                  <TableHead className="text-right">c</TableHead>
                  <TableHead className="text-right">done</TableHead>
                  <TableHead className="text-right">ttft p50</TableHead>
                  <TableHead className="text-right">lat p95</TableHead>
                  <TableHead className="text-right">decode tok/s</TableHead>
                  <TableHead className="text-right">throughput</TableHead>
                  <TableHead className="text-right">errors</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map(r => (
                  <TableRow key={r.run_id}>
                    <TableCell className="font-mono">{r.model}</TableCell>
                    <TableCell><Badge variant="outline">{r.runner}</Badge></TableCell>
                    <TableCell className="text-right tabular-nums">{r.concurrency}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.completed}/{r.count}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.ttft_p50_ms}ms</TableCell>
                    <TableCell className="text-right tabular-nums">{r.latency_p95_ms}ms</TableCell>
                    <TableCell className="text-right tabular-nums">{r.decode_tok_s_avg}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.throughput_tok_s}</TableCell>
                    <TableCell className={`text-right tabular-nums ${r.errors > 0 ? "text-destructive" : ""}`}>{r.errors}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
