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
  /** when it began to need you (ISO, UTC): pushed, or listed for a quiet one */
  at: string
  claude: string
}

/** A session that answered, cache still warm: the band's, never an inbox row. */
export type SummaryAnswered = {
  subject: string
  ref: string
  title: string
  url: string
  /** when it answered (ISO, UTC) */
  at: string
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
  answered: SummaryAnswered[]
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
