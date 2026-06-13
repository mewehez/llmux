import { useState, useRef, useEffect } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

type Model = "135m" | "360m"
type Status = "idle" | "sending" | "streaming" | "done" | "error"

interface Message {
  role: "user" | "assistant"
  content: string
  model?: Model
  taskId?: string
}

function StatusBadge({ status }: { status: Status }) {
  const map: Record<Status, { label: string; className: string }> = {
    idle:      { label: "idle",      className: "" },
    sending:   { label: "sending",   className: "border-blue-500 text-blue-600" },
    streaming: { label: "streaming", className: "border-yellow-500 text-yellow-600" },
    done:      { label: "done",      className: "border-green-500 text-green-600" },
    error:     { label: "error",     className: "border-red-500 text-red-600" },
  }
  const { label, className } = map[status]
  return (
    <Badge variant="outline" className={className}>
      {label}
    </Badge>
  )
}

export function MessageSender() {
  const [message, setMessage]   = useState("")
  const [model, setModel]       = useState<Model>("135m")
  const [status, setStatus]     = useState<Status>("idle")
  const [messages, setMessages] = useState<Message[]>([])
  const [error, setError]       = useState<string | null>(null)
  const bottomRef               = useRef<HTMLDivElement>(null)
  const eventSourceRef          = useRef<EventSource | null>(null)

  // Auto-scroll to bottom on new content
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  // Cleanup SSE on unmount
  useEffect(() => {
    return () => eventSourceRef.current?.close()
  }, [])

  async function send() {
    if (!message.trim() || status === "streaming") return

    const userMessage = message.trim()
    setMessage("")
    setError(null)
    setStatus("sending")

    // Add user message to history
    setMessages(prev => [...prev, { role: "user", content: userMessage }])

    // Add empty assistant message that we'll fill token by token
    setMessages(prev => [...prev, {
      role:   "assistant",
      content: "",
      model,
    }])

    try {
      // 1. POST /chat → get task_id
      const res = await fetch(`${API_URL}/chat`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ message: userMessage, model }),
      })

      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const { task_id } = await res.json()

      setStatus("streaming")

      // 2. Open SSE connection
      const es = new EventSource(`${API_URL}/sse/${task_id}`)
      eventSourceRef.current = es

      es.onmessage = (event) => {
        const payload = JSON.parse(event.data)

        if (payload.type === "token") {
          // Append token to the last assistant message
          setMessages(prev => {
            const updated = [...prev]
            const last    = updated[updated.length - 1]
            if (last.role === "assistant") {
              updated[updated.length - 1] = {
                ...last,
                content: last.content + payload.content,
                taskId:  task_id,
              }
            }
            return updated
          })
        }

        if (payload.type === "done") {
          setStatus("done")
          es.close()
          // Reset to idle after a beat so the badge is visible
          setTimeout(() => setStatus("idle"), 2000)
        }

        if (payload.type === "error") {
          setError(payload.content)
          setStatus("error")
          es.close()
        }
      }

      es.onerror = () => {
        setError("SSE connection lost")
        setStatus("error")
        es.close()
      }

    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
      setStatus("error")
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  function clearHistory() {
    setMessages([])
    setStatus("idle")
    setError(null)
  }

  return (
    <div className="space-y-4">

      {/* Model selector */}
      <div className="flex gap-3 items-center">
        <Label>Model</Label>
        {(["135m", "360m"] as Model[]).map(m => (
          <button
            key={m}
            onClick={() => setModel(m)}
            className={[
              "px-3 py-1 rounded-md text-sm font-medium border transition-colors",
              model === m
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-background text-muted-foreground border-border hover:border-primary",
            ].join(" ")}
          >
            SmolLM2-{m.toUpperCase()}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <StatusBadge status={status} />
          {messages.length > 0 && (
            <Button variant="ghost" size="sm" onClick={clearHistory}>
              Clear
            </Button>
          )}
        </div>
      </div>

      <Separator />

      {/* Message history */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-muted-foreground">
            Conversation
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-3 max-h-96 overflow-y-auto pr-2">
            {messages.length === 0 && (
              <p className="text-sm text-muted-foreground italic text-center py-8">
                No messages yet. Send one below.
              </p>
            )}

            {messages.map((msg, i) => (
              <div
                key={i}
                className={[
                  "rounded-lg px-3 py-2 text-sm max-w-[85%]",
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground ml-auto"
                    : "bg-muted text-foreground",
                ].join(" ")}
              >
                {msg.role === "assistant" && (
                  <p className="text-xs text-muted-foreground mb-1 font-mono">
                    SmolLM2-{msg.model?.toUpperCase()}
                    {msg.taskId && ` · ${msg.taskId.slice(0, 8)}`}
                  </p>
                )}
                <p className="whitespace-pre-wrap">
                  {msg.content}
                  {/* Blinking cursor while streaming */}
                  {status === "streaming" &&
                    i === messages.length - 1 &&
                    msg.role === "assistant" && (
                      <span className="animate-pulse">▋</span>
                    )}
                </p>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </CardContent>
      </Card>

      {error && (
        <div className="rounded-md bg-destructive/10 text-destructive px-4 py-2 text-sm">
          {error}
        </div>
      )}

      {/* Input */}
      <div className="flex gap-2">
        <Input
          placeholder="Type a message... (Enter to send)"
          value={message}
          onChange={e => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={status === "streaming"}
        />
        <Button
          onClick={send}
          disabled={!message.trim() || status === "streaming"}
        >
          {status === "streaming" ? "Streaming..." : "Send"}
        </Button>
      </div>
    </div>
  )
}