import * as React from "react"
import * as Cb from "@radix-ui/react-checkbox"
import { Check } from "lucide-react"
import { cn } from "@/lib/utils"

export const Checkbox = React.forwardRef<
  React.ElementRef<typeof Cb.Root>,
  React.ComponentPropsWithoutRef<typeof Cb.Root>
>(({ className, ...props }, ref) => (
  <Cb.Root
    ref={ref}
    className={cn(
      "peer h-4 w-4 shrink-0 rounded-sm border border-input focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground",
      className
    )}
    {...props}
  >
    <Cb.Indicator className={cn("flex items-center justify-center text-current")}>
      <Check className="h-3 w-3" strokeWidth={3} />
    </Cb.Indicator>
  </Cb.Root>
))
Checkbox.displayName = Cb.Root.displayName
