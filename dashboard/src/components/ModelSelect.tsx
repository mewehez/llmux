import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfig } from "@/lib/config"
import type { ModelSelection } from "@/lib/types"

export function ModelSelect({
  value,
  onChange,
  includeAuto = false,
  disabled,
}: {
  value: ModelSelection
  onChange: (model: ModelSelection) => void
  includeAuto?: boolean
  disabled?: boolean
}) {
  const { models } = useConfig()
  return (
    <Select value={value} onValueChange={(v) => onChange(v as ModelSelection)} disabled={disabled}>
      <SelectTrigger>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {includeAuto && <SelectItem value="auto">Auto</SelectItem>}
        {models.map(m => (
          <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
