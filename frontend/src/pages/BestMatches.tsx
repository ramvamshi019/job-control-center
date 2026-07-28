import { Sparkles } from "lucide-react"
import { JobFeed } from "@/components/JobFeed"

export default function BestMatches() {
  return (
    <JobFeed
      title="Best Matches"
      icon={<Sparkles className="h-5 w-5 text-primary" />}
      description="Full applyable backlog — sponsor-safe, US, on-target roles you haven't applied to yet, ranked by match score."
      defaultMinScore={40}
      defaultFreshDays={14}
      orderBy="score"
      defaultSort={[{ id: "match_score", desc: true }]}
      dateMode="seen"
    />
  )
}
