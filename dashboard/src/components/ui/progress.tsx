import * as React from "react"

import { cn } from "@/lib/utils"

function Progress({
  className,
  indicatorClassName,
  value = 0,
  ...props
}: React.ComponentProps<"div"> & { value?: number; indicatorClassName?: string }) {
  const pct = Math.max(0, Math.min(100, value))
  return (
    <div
      data-slot="progress"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      className={cn("relative h-2 w-full overflow-hidden rounded-full bg-muted", className)}
      {...props}
    >
      <div
        data-slot="progress-indicator"
        className={cn("h-full rounded-full bg-primary transition-all", indicatorClassName)}
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

export { Progress }
