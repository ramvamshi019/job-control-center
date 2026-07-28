import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-1.5 py-0.5 text-xs font-medium leading-none",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary/10 text-primary",
        sponsor: "border-transparent bg-emerald-500/10 text-emerald-400",
        risk: "border-transparent bg-amber-500/10 text-amber-400",
        risk_high: "border-transparent bg-rose-500/10 text-rose-400",
        muted: "border-transparent bg-muted text-muted-foreground",
        outline: "border-input text-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
