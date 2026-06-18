import type React from "react"
import { useEffect, useRef, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { ModelSelect } from "@/components/ModelSelect"
import { simulateChatStream } from "@/lib/mock"
import { MOCK, postChat, streamChat } from "@/lib/api"
import type { ChatMessage, EventItem, Model, ModelSelection, WorkerMetrics } from "@/lib/types"

type Status = "idle" | "streaming" | "done"

export function ChatPanel({
  workers,
  onThroughput,
  onEvent,
  messages,
  setMessages,
}: {
  workers: WorkerMetrics[]
  onThroughput: (tokensPerSec: number) => void
  onEvent: (event: EventItem) => void
  // Lifted to App so chat history survives tab switches.
  messages: ChatMessage[]
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>
}) {
  const [model, setModel]       = useState<ModelSelection>("auto")
  const [input, setInput]       = useState("")
  const [status, setStatus]     = useState<Status>("idle")
  const bottomRef               = useRef<HTMLDivElement>(null)
  const cancelRef               = useRef<(() => void) | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  useEffect(() => () => cancelRef.current?.(), [])

  function send() {
    const text = input.trim()
    if (!text || status === "streaming") return

    setInput("")
    setStatus("streaming")
    setMessages(prev => [...prev, { role: "user", content: text }, { role: "assistant", content: "", model }])

    onEvent({ id: `ev-${Date.now()}`, time: new Date().toLocaleTimeString(), type: "request", message: `chat request → ${model === "auto" ? "auto" : `SmolLM2-${model}`}` })

    const appendToken = (token: string) => {
      setMessages(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        updated[updated.length - 1] = { ...last, content: last.content + token }
        return updated
      })
    }

    const finish = ({ workerId, llmPod, model: resolvedModel, tokens, elapsedMs }: { workerId: string; llmPod: string; model: Model; tokens: number; elapsedMs: number }) => {
      setMessages(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        updated[updated.length - 1] = { ...last, model: resolvedModel, workerId, llmPod, tokens, elapsedMs }
        return updated
      })
      onThroughput(tokens / (elapsedMs / 1000))
      onEvent({ id: `ev-${Date.now()}-done`, time: new Date().toLocaleTimeString(), type: "complete", message: `worker ${workerId} → ${llmPod} · ${(elapsedMs / 1000).toFixed(1)}s` })
      setStatus("done")
      setTimeout(() => setStatus("idle"), 1000)
    }

    const fallback = () => {
      // Simulate only in explicit mock mode; otherwise surface the failure
      // honestly instead of streaming fake tokens.
      if (MOCK) {
        cancelRef.current = simulateChatStream(model, workers, appendToken, finish)
        return
      }
      setMessages(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        updated[updated.length - 1] = { ...last, content: (last.content || "") + " ⚠ API unreachable" }
        return updated
      })
      setStatus("idle")
    }

    postChat(text, model)
      .then(({ task_id, model: resolvedModel }) => {
        cancelRef.current = streamChat(
          task_id,
          resolvedModel,
          appendToken,
          finish,
          () => fallback(),
        )
      })
      .catch(fallback)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">Model</span>
        <ModelSelect value={model} onChange={setModel} includeAuto />
        <Badge variant="outline" className="ml-auto">{status}</Badge>
      </div>

      <Card>
        <CardHeader><CardTitle>Conversation</CardTitle></CardHeader>
        <CardContent>
          <div className="max-h-96 space-y-3 overflow-y-auto pr-2">
            {messages.length === 0 && (
              <p className="py-8 text-center text-sm italic text-muted-foreground">
                No messages yet. Send one below to stream a response.
              </p>
            )}
            {messages.map((msg, i) => (
              <div
                key={i}
                className={[
                  "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                  msg.role === "user" ? "ml-auto bg-primary text-primary-foreground" : "bg-muted",
                ].join(" ")}
              >
                {msg.role === "assistant" && (
                  <p className="mb-1 font-mono text-xs text-muted-foreground">
                    SmolLM2-{msg.model}
                    {msg.workerId && ` · worker ${msg.workerId}`}
                    {msg.llmPod && ` → ${msg.llmPod}`}
                    {msg.tokens != null && ` · ${msg.tokens} tok · ${(msg.elapsedMs! / 1000).toFixed(1)}s`}
                  </p>
                )}
                <p className="whitespace-pre-wrap">
                  {msg.content}
                  {status === "streaming" && i === messages.length - 1 && msg.role === "assistant" && (
                    <span className="animate-pulse">▋</span>
                  )}
                </p>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </CardContent>
      </Card>

      <div className="flex gap-2">
        <Input
          placeholder="Type a message... (Enter to send)"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={status === "streaming"}
        />
        <Button onClick={send} disabled={!input.trim() || status === "streaming"}>
          {status === "streaming" ? "Streaming..." : "Send"}
        </Button>
      </div>
    </div>
  )
}
