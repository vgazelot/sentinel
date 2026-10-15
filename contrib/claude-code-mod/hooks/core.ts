import type { Summary, SummaryNotification } from '../types'

export const DEFAULT_URL = 'http://localhost:8300'
export const DEEP_PROMPT =
  "Review {url} as a devil's advocate: durability, duplication, modeling, blast radius. Read-only, post nothing on GitHub."
const MAX_TOASTS = 3

/** The status line: what waits on you first, then what is worth knowing. */
export function statusLine(summary: Summary | null, error: string): string {
  if (!summary) return error ? 'Sentinel offline' : 'Sentinel …'
  const parts = [summary.attention ? `● ${summary.attention} for you` : '✓ nothing for you']
  if (summary.counts.broken) parts.push(`⚠ ${summary.counts.broken} broke after merge`)
  if (summary.counts.health) parts.push('⚠ a source is down')
  if (summary.counts.deps) parts.push(`${summary.counts.deps} bump${summary.counts.deps > 1 ? 's' : ''}`)
  if (summary.agents.running) parts.push('Claude reading')
  return `Sentinel ${parts.join(' · ')}`
}

/** Toast lines for the notifications Sentinel sent since the last poll: a few, then a count. */
export function toasts(notifications: SummaryNotification[]): string[] {
  const lines = notifications.slice(0, MAX_TOASTS).map(n => (n.body ? `${n.title} — ${n.body}` : n.title))
  if (notifications.length > MAX_TOASTS) lines.push(`and ${notifications.length - MAX_TOASTS} more in Sentinel`)
  return lines
}

export function prompt(template: string, url: string): string {
  return template.split('{url}').join(url)
}

/** The PR lanes, where a review in the session makes sense. */
export function isReview(lane: string): boolean {
  return lane === 'you' || lane === 'team'
}
