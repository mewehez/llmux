import { useEffect, useState } from "react"
import { API_URL } from "./api"
import type { Model } from "./types"

/** A model as advertised by the API's /config (the registry, public view). */
export interface ModelInfo {
  id:          Model
  label:       string
  runner:      string
  maxReplicas: number
  maxLlmPods:  number
}

/** Used until the API answers /config (or when it's unreachable) so the UI
 *  still renders. Mirrors config/models.json's defaults. */
export const FALLBACK_MODELS: ModelInfo[] = [
  { id: "135m", label: "SmolLM2-135M", runner: "llamacpp", maxReplicas: 3, maxLlmPods: 2 },
  { id: "360m", label: "SmolLM2-360M", runner: "llamacpp", maxReplicas: 2, maxLlmPods: 2 },
]

export async function fetchConfig(): Promise<ModelInfo[]> {
  const res = await fetch(`${API_URL}/config`)
  if (!res.ok) throw new Error(`config: ${res.status}`)
  const data = await res.json()
  return data.models as ModelInfo[]
}

/**
 * Loads the model registry from the API once. Falls back to FALLBACK_MODELS
 * (and isLive=false) when the API is unreachable, so the dashboard never
 * hardcodes the model set yet still renders offline.
 */
export function useConfig(): { models: ModelInfo[]; isLive: boolean } {
  const [models, setModels] = useState<ModelInfo[]>(FALLBACK_MODELS)
  const [isLive, setLive]   = useState(false)

  useEffect(() => {
    let cancelled = false
    fetchConfig()
      .then(m => { if (!cancelled && m?.length) { setModels(m); setLive(true) } })
      .catch(() => { if (!cancelled) setLive(false) })
    return () => { cancelled = true }
  }, [])

  return { models, isLive }
}
