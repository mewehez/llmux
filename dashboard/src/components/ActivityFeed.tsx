import { Fragment, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { useEvents, type LiveEvent } from "@/lib/events"

const TYPE_BADGE: Record<string, string> = {
  task_started:   "border-blue-500 text-blue-600",
  task_completed: "border-green-500 text-green-600",
  task_error:     "border-destructive text-destructive",
  task_dead:      "border-destructive text-destructive",
  worker_up:      "border-green-500 text-green-600",
  worker_down:    "border-yellow-500 text-yellow-600",
  scale_up:       "border-green-500 text-green-600",
  scale_down:     "border-yellow-500 text-yellow-600",
}

function describe(e: LiveEvent): string {
  const w = e.worker_id ?? ""
  switch (e.type) {
    case "task_completed": return `${w} · ${e.tokens ?? "?"} tok · ${e.tok_s ?? "?"} tok/s · ttft ${e.ttft_ms ?? "?"}ms`
    case "task_started":   return `${w} → ${e.task_id?.slice(0, 8) ?? ""}`
    case "task_error":     return `${w}: ${e.error || "(no message)"}`
    case "task_dead":      return `${w}: dead-lettered — ${e.reason ?? ""}`
    case "worker_up":      return `${w} joined`
    case "worker_down":    return `${w} left`
    case "scale_up":
    case "scale_down":     return `${e.component ?? ""} ${e.from ?? "?"} → ${e.to ?? "?"}`
    default:               return e.task_id ?? ""
  }
}

function clock(ts: string): string {
  const n = Number(ts)
  return Number.isFinite(n) ? new Date(n).toLocaleTimeString() : ""
}

// Fields shown in the expanded detail view, in order (only non-empty ones render).
const DETAIL_FIELDS: { key: keyof LiveEvent; label: string }[] = [
  { key: "task_id", label: "task" },
  { key: "worker_id", label: "worker" },
  { key: "llm", label: "llm pod" },
  { key: "runner", label: "runner" },
  { key: "tokens", label: "tokens" },
  { key: "ttft_ms", label: "ttft (ms)" },
  { key: "tok_s", label: "tok/s" },
  { key: "latency_ms", label: "latency (ms)" },
  { key: "component", label: "component" },
  { key: "from", label: "from" },
  { key: "to", label: "to" },
  { key: "error", label: "error" },
]

/** Real-time activity feed backed by the server's llm:events stream (/events/stream).
 *  Click a row to expand full task detail. */
export function ActivityFeed() {
  const { events, isLive } = useEvents()
  const [expanded, setExpanded] = useState<string | null>(null)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Activity</CardTitle>
        <Badge
          variant="outline"
          className={isLive ? "border-green-500 text-green-600" : "border-border text-muted-foreground"}
        >
          {isLive ? "live" : "disconnected"}
        </Badge>
      </CardHeader>
      <CardContent>
        <div className="max-h-[28rem] overflow-y-auto">
          {events.length === 0 ? (
            <p className="py-6 text-center text-sm italic text-muted-foreground">Waiting for events…</p>
          ) : (
            <Table>
              <TableBody>
                {events.map(e => (
                  <Fragment key={e.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() => setExpanded(expanded === e.id ? null : e.id)}
                    >
                      <TableCell className="w-20 font-mono text-[10px] text-muted-foreground">{clock(e.ts)}</TableCell>
                      <TableCell className="w-28">
                        <Badge variant="outline" className={TYPE_BADGE[e.type] ?? ""}>{e.type}</Badge>
                      </TableCell>
                      <TableCell className="w-12 font-mono text-[10px]">{e.model ?? ""}</TableCell>
                      <TableCell className="truncate font-mono text-[11px] text-muted-foreground">{describe(e)}</TableCell>
                    </TableRow>
                    {expanded === e.id && (
                      <TableRow>
                        <TableCell colSpan={4} className="bg-muted/30">
                          <div className="grid grid-cols-1 gap-x-6 gap-y-1 px-2 py-1 sm:grid-cols-2">
                            <div className="flex gap-2 text-[11px]">
                              <span className="w-20 shrink-0 text-muted-foreground">timestamp</span>
                              <span className="font-mono">{Number.isFinite(Number(e.ts)) ? new Date(Number(e.ts)).toLocaleString() : e.ts}</span>
                            </div>
                            <div className="flex gap-2 text-[11px]">
                              <span className="w-20 shrink-0 text-muted-foreground">type</span>
                              <span className="font-mono">{e.type}</span>
                            </div>
                            {DETAIL_FIELDS.map(({ key, label }) => {
                              const v = e[key]
                              if (v === undefined || v === null || v === "") return null
                              return (
                                <div key={key} className="flex gap-2 text-[11px]">
                                  <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
                                  <span className="font-mono break-all">{String(v)}</span>
                                </div>
                              )
                            })}
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
