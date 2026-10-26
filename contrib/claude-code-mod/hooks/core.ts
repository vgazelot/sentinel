import type { Summary, SummaryNotification } from '../types'

export const DEFAULT_URL = 'http://localhost:8300'
export const DEEP_PROMPT =
  "Review {url} as a devil's advocate: durability, duplication, modeling, blast radius. Read-only, post nothing on GitHub."
const MAX_TOASTS = 3
const MAX_BAND = 3

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

/** The band above the prompt: what began to need you since your last prompt in this session (`since`, ms), then the
 *  other sessions that answered meanwhile — this very session left out; a few lines, then how many more. */
export function band(summary: Summary, since: number, own: string): { lines: string[]; more: number; actionable: boolean } {
  const after = (at: string) => Date.parse(at) > since
  const items = summary.items.filter(it => after(it.at) && !(it.lane === 'session' && it.subject === own))
  const fresh = [
    ...items.map(it => [it.ref, it.title].filter(Boolean).join(' · ') + (it.why ? ` — ${it.why}` : '')),
    // an older Sentinel (the mod lands with the merge, the app with its rebuild) sends no `answered`
    ...(summary.answered ?? []).filter(s => after(s.at) && s.subject !== own)
      .map(s => [s.ref, s.title].filter(Boolean).join(' · ') + ' — answered'),
  ]
  // the inbox rows are what /sentinel acts on; a session that answered is opened where it runs
  return { lines: fresh.slice(0, MAX_BAND), more: Math.max(fresh.length - MAX_BAND, 0), actionable: items.length > 0 }
}

/** A prompt you sent — typed here, or through a remote client — and not one the session got on its own (a background
 *  task done, a teammate, a schedule): only yours moves what the band counts from. */
export function fromYou(origin: string): boolean {
  return origin === 'composer' || origin === 'bridge'
}

export function prompt(template: string, url: string): string {
  return template.split('{url}').join(url)
}

/** The PR lanes, where a review in the session makes sense. */
export function isReview(lane: string): boolean {
  return lane === 'you' || lane === 'team'
}
