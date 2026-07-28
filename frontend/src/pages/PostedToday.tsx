import { Flame } from "lucide-react"
import { JobFeed } from "@/components/JobFeed"

/**
 * Posted Today = jobs the crawler first saw in the last 24 hours.
 *
 * Server-side we ask `discovered_within_hours=24`. That's a slightly looser
 * definition than the dashboard's "posted-date confirmed today" (which drops
 * iCIMS/SmartRecruiters/Workday whose posted_at is a crawl-time fallback) --
 * intentionally, so Ram sees fresh-on-a-known-board jobs from those sources
 * too. Sorted by discovery time so the very latest bubble to the top.
 *
 * Min score defaults to 30 (looser than Best Matches' 40) because "fresh"
 * is worth surfacing even at a lower fit signal -- a 32 today may still be
 * worth a look before it ages out.
 */
export default function PostedToday() {
  return (
    <JobFeed
      title="Posted Today"
      icon={<Flame className="h-5 w-5 text-rose-400" />}
      description="Jobs first seen by the crawler in the last 24 hours. Get in early before the queue fills."
      defaultMinScore={30}
      defaultFreshDays={1}
      orderBy="discovered_at"
      defaultSort={[{ id: "discovered_at", desc: true }]}
      dateMode="posted"
      showFreshnessSlider={false}
    />
  )
}
