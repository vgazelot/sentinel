/** The context advisor's decisions, stateless: the session's figures in, text out. */

/** $ per million input tokens: cache read, 5-minute cache write, 1-hour cache write. */
type Prices = { read: number; write5m: number; write1h: number }

const OPUS_5_5: Prices = { read: 0.2, write5m: 5, write1h: 8 }

const PRICES: Record<string, Prices> = {
  'claude-opus-5-5': OPUS_5_5,
  'claude-opus-5': { read: 0.5, write5m: 6.25, write1h: 10 },
  'claude-sonnet-5-5': { read: 0.2, write5m: 2.5, write1h: 4 },
  'claude-haiku-4-5': { read: 0.1, write5m: 1.25, write1h: 2 },
}

export function prices(model: string): Prices {
  const id = model.replace(/\[.*\]$/, '').replace(/-\d{8}$/, '')
  return PRICES[id] ?? OPUS_5_5
}

export type Settings = {
  /** Past this, a handoff is offered at the end of the turn. */
  handoffAt: number
  /** Below this, a cold cache is not worth a warning. */
  coldMin: number
  /** Lifetime of the prompt cache, in minutes. */
  ttlMin: number
  /** Size of a fresh session (system prompt, tools, CLAUDE.md, memory). */
  freshTokens: number
  /** Hands off on its own when the answer does not end with a question. */
  auto: boolean
}

export const DEFAULTS: Settings = { handoffAt: 350_000, coldMin: 150_000, ttlMin: 60, freshTokens: 72_000, auto: false }

export function settingsFrom(env: Record<string, string | undefined>): Settings {
  const num = (v: string | undefined, d: number) => (v && Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : d)
  return {
    handoffAt: num(env.CONTEXT_HANDOFF_TOKENS, DEFAULTS.handoffAt),
    coldMin: num(env.CONTEXT_COLD_MIN_TOKENS, DEFAULTS.coldMin),
    ttlMin: num(env.CONTEXT_CACHE_TTL_MIN, DEFAULTS.ttlMin),
    freshTokens: num(env.CONTEXT_FRESH_TOKENS, DEFAULTS.freshTokens),
    auto: env.CONTEXT_AUTO === '1',
  }
}

export function isCold(lastResponseAt: number | null, now: number, ttlMin: number): boolean {
  return lastResponseAt !== null && now - lastResponseAt > ttlMin * 60_000
}

/** What the first call of the next message costs: reading the context again (warm cache) or writing it again (cold). */
export function nextSendUsd(tokens: number, model: string, cold: boolean): number {
  const p = prices(model)
  return (tokens / 1e6) * (cold ? p.write1h : p.read)
}

export function freshUsd(s: Settings, model: string): number {
  return (s.freshTokens / 1e6) * prices(model).write1h
}

export const kTokens = (n: number) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`)
export const usd = (n: number) => `$${n.toFixed(2)}`

export function since(ms: number): string {
  const m = Math.floor(ms / 60_000)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, '0')}`
  return `${Math.floor(h / 24)} d`
}

const PATTERNS = [
  /\bsc-(\d{3,7})\b/gi,
  /shortcut\.com\/[^\s/]+\/story\/(\d{3,7})/gi,
  /"?story_?public_?id"?\s*[:=]\s*"?(\d{3,7})/gi,
]

/** The last story a text names, as `sc-12345`. */
export function findStory(text: string): string | null {
  let best: { at: number; id: string } | null = null
  for (const re of PATTERNS) {
    for (const m of text.matchAll(re)) {
      const id = m[1]
      if (id && (!best || (m.index ?? 0) >= best.at)) best = { at: m.index ?? 0, id }
    }
  }
  return best ? `sc-${best.id}` : null
}

/** The story a tool call points at: a tool of the Shortcut MCP server, or a command that names it. */
export function storyFromTool(tool: string, args: Record<string, unknown>): string | null {
  if (/shortcut/i.test(tool)) {
    const id = args.storyPublicId ?? args.story_public_id ?? args.storyId
    if (typeof id === 'number' || (typeof id === 'string' && /^\d{3,7}$/.test(id))) return `sc-${id}`
  }
  return tool === 'Bash' && typeof args.command === 'string' ? findStory(args.command) : null
}

export type View = {
  tokens: number
  cold: boolean
  quota5h?: number
  story: string | null
  handoffAt: number
}

export function statusText(v: View): string {
  const parts = [v.tokens ? kTokens(v.tokens) + (v.cold ? ' · cold' : '') : 'fresh context']
  if (v.tokens >= v.handoffAt) parts.push(v.story ? '/handoff advised' : 'big context, no ticket')
  if (v.quota5h !== undefined) parts.push(`5 h quota ${Math.round(v.quota5h)}%`)
  if (v.story && v.tokens < v.handoffAt) parts.push(v.story)
  return parts.join(' · ')
}

export type Guard = { tokens: number; model: string; idleMs: number; story: string | null; settings: Settings }

export function guardText(g: Guard): string {
  const next = usd(nextSendUsd(g.tokens, g.model, true))
  const fresh = usd(freshUsd(g.settings, g.model))
  const ways = g.story
    ? `/fresh to start over from ${g.story} if it is up to date (≈ ${fresh}); /handoff to write the state into it first (≈ ${next} then ${fresh})`
    : `/clear to start from scratch (≈ ${fresh}); no story linked, /ticket sc-12345 to link one`
  return `Cold cache: the session has slept for ${since(g.idleMs)} with ${kTokens(g.tokens)} of context. This message would cost ≈ ${next} just to read it again. Enter to send it anyway (your text is back in the prompt); ${ways}.`
}

export function handoffPrompt(story: string, tokens: number): string {
  return [
    `Hand off: the context is at ${kTokens(tokens)}, a fresh session will pick up from story ${story}.`,
    `Write in ${story}, in English, a comment titled "Handoff" that is enough to pick up without losing anything: Done, Left to do, Next step, Decisions and pitfalls, Branches, PRs and key files.`,
    'Ticket style: short bullets, concrete references. Update the story tasks too if needed. Do nothing else.',
    `If ${story} is not the story of the current work, say so and stop, without writing anything or the final line.`,
    `End with a single line: HANDOFF-OK ${story}`,
  ].join('\n')
}

export function resumePrompt(story: string): string {
  return `Pick up ${story}: read the story and its latest "Handoff" comment, check the linked branch and PR, sum up in two lines where things stand, then carry on from the next step.`
}

export function compactContext(story: string): string {
  return `The conversation was just compacted. The durable state of this work is in story ${story} (its latest "Handoff" comment, if any): read it when a detail is missing.`
}

export type Report = {
  tokens: number
  window: number
  model: string
  cold: boolean
  idleMs: number | null
  costUsd?: number
  quota: Record<string, number>
  story: string | null
  pinned: boolean
  settings: Settings
}

export function report(r: Report): string {
  const lines = []
  const pct = r.window ? ` (${Math.round((100 * r.tokens) / r.window)}% of ${kTokens(r.window)})` : ''
  const cache = r.idleMs === null ? 'cache unknown' : r.cold ? `cache cold for ${since(r.idleMs)}` : 'cache warm'
  lines.push(`Context: ${kTokens(r.tokens)}${pct} · ${cache}`)
  lines.push(`Next send ≈ ${usd(nextSendUsd(r.tokens, r.model, r.cold))} · fresh ≈ ${usd(freshUsd(r.settings, r.model))}`)
  const quota = (['five_hour', 'seven_day'] as const).flatMap(k => {
    const used = r.quota[k]
    return used === undefined ? [] : [`${k === 'five_hour' ? '5 h' : '7 d'} ${Math.round(used)}%`]
  })
  lines.push([r.costUsd !== undefined ? `Session ≈ ${usd(r.costUsd)}` : null, quota.length ? `quota ${quota.join(', ')}` : null].filter(Boolean).join(' · '))
  lines.push(r.story
    ? `Story: ${r.story}${r.pinned ? '' : ' (detected)'} · /handoff writes the state into it then starts fresh, /fresh starts fresh from it right away`
    : 'No story linked: /ticket sc-12345 to be able to start fresh without losing anything')
  return lines.filter(Boolean).join('\n')
}
