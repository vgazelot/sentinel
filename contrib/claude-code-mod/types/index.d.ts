/** What Sentinel's GET /api/summary answers (only the fields the mod reads). */
export type SummaryItem = {
  id: number
  lane: string
  ref: string
  title: string
  url: string
  why: string
  since: string
  subject: string
  claude: string
}

export type SummaryNotification = {
  id: number
  title: string
  body: string | null
  url: string
  reason: string
  lane: string
}

export type Summary = {
  attention: number
  counts: Record<string, number>
  landing: Record<string, number>
  items: SummaryItem[]
  notifications: SummaryNotification[]
  seq: number
  agents: { running: number; queued: number }
  base_url: string
}

declare module 'claude-code' {
  interface PluginState {
    sentinel: { summary: Summary | null; error: string }
  }
}
