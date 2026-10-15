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

/** A typed `/load-project mesh` or `/resume-project mesh` (a project-memory plugin): the project's short name. */
export function projectFromPrompt(text: string): string | null {
  return text.match(/^\/(?:[\w-]+:)?(?:load|resume)-project\s+([\w.-]+)/)?.[1] ?? null
}

/** The same, when the model loads the project itself through the Skill tool. */
export function projectFromTool(tool: string, args: Record<string, unknown>): string | null {
  if (tool !== 'Skill' || typeof args.skill !== 'string' || !/(^|:)(load|resume)-project$/.test(args.skill)) return null
  const first = typeof args.args === 'string' ? (args.args.trim().split(/\s+/)[0] ?? '') : ''
  return /^[\w.-]+$/.test(first) ? first : null
}

/** Where a fresh session picks up from: the project's memory, the story, or both. */
export type Anchor = { project: string | null; story: string | null }

export function where(a: Anchor): string {
  return [a.project && `project ${a.project}`, a.story].filter(Boolean).join(' and ')
}

export type View = Anchor & {
  tokens: number
  cold: boolean
  quota5h?: number
  handoffAt: number
}

export function statusText(v: View): string {
  const parts = [v.tokens ? kTokens(v.tokens) + (v.cold ? ' · cold' : '') : 'fresh context']
  const anchored = Boolean(v.project || v.story)
  if (v.tokens >= v.handoffAt) parts.push(anchored ? '/handoff advised' : 'big context, nothing to hand off to')
  if (v.quota5h !== undefined) parts.push(`5 h quota ${Math.round(v.quota5h)}%`)
  if (anchored && v.tokens < v.handoffAt) parts.push(...[v.project, v.story].filter((x): x is string => Boolean(x)))
  return parts.join(' · ')
}

export type Guard = Anchor & { tokens: number; model: string; idleMs: number; settings: Settings }

const LINK = '/load-project <name> or /ticket sc-12345'

export function guardText(g: Guard): string {
  const next = usd(nextSendUsd(g.tokens, g.model, true))
  const fresh = usd(freshUsd(g.settings, g.model))
  const ways = g.project || g.story
    ? `/fresh to start over from ${where(g)} if it is up to date (≈ ${fresh}); /handoff to write the state there first (≈ ${next} then ${fresh})`
    : `/clear to start from scratch (≈ ${fresh}); nothing linked, ${LINK} to link one`
  return `Cold cache: the session has slept for ${since(g.idleMs)} with ${kTokens(g.tokens)} of context. This message would cost ≈ ${next} just to read it again. Enter to send it anyway (your text is back in the prompt); ${ways}.`
}

/** The marker the session ends its handoff with: the project's name, else the story's. */
export const key = (a: Anchor) => a.project ?? a.story ?? ''

export function handoffPrompt(a: Anchor, tokens: number): string {
  const lines = [`Hand off: the context is at ${kTokens(tokens)}, a fresh session will pick up from ${where(a)}.`]
  if (a.project) {
    lines.push(
      `In the memory of project ${a.project} (loaded in this session with /load-project or /resume-project), do the doc sync of /wrapup-project: Decision Log, Open Questions, the Summary status and a Changelog entry for this session; edit sections, never rewrite whole files.`,
      `Then overwrite its handoff.md: "# Handoff — <today>", "## Next session" (2-3 concrete next actions, with pointers into the project's files), "## Open threads from this session". Ask nothing: this replaces the handoff interview of /wrapup-project. A shared project: commit and push nothing, that stays with a manual /wrapup-project.`,
    )
  }
  if (a.story) {
    lines.push(
      `Write in ${a.story}, in English, a comment titled "Handoff" that is enough to pick up without losing anything: Done, Left to do, Next step, Decisions and pitfalls, Branches, PRs and key files.`,
      'Ticket style: short bullets, concrete references. Update the story tasks too if needed.',
    )
  }
  lines.push(
    'Do nothing else.',
    `If ${where(a)} is not what the current work is about, say so and stop, without writing anything or the final line.`,
    `End with a single line: HANDOFF-OK ${key(a)}`,
  )
  return lines.join('\n')
}

export function resumePrompt(a: Anchor): string {
  const steps = [
    a.project && `run the resume-project skill with "${a.project}" (it loads the project memory and its handoff.md)`,
    a.story && `read ${a.story} and its latest "Handoff" comment, check the linked branch and PR`,
  ].filter(Boolean)
  return `Pick up ${where(a)}: ${steps.join('; then ')}. Sum up in two lines where things stand, then carry on from the next step.`
}

export function compactContext(a: Anchor): string {
  const homes = [a.project && `the memory of project ${a.project} (summary.md, handoff.md)`, a.story && `story ${a.story} (its latest "Handoff" comment, if any)`]
  return `The conversation was just compacted. The durable state of this work is in ${homes.filter(Boolean).join(' and ')}: read it when a detail is missing.`
}

export type Report = Anchor & {
  tokens: number
  window: number
  model: string
  cold: boolean
  idleMs: number | null
  costUsd?: number
  quota: Record<string, number>
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
  const story = r.story && `${r.story}${r.pinned ? '' : ' (detected)'}`
  lines.push(r.project || r.story
    ? `Picks up from: ${where({ project: r.project, story })} · /handoff writes the state there then starts fresh, /fresh starts fresh from it right away`
    : `Nothing linked: ${LINK} to be able to start fresh without losing anything`)
  return lines.filter(Boolean).join('\n')
}
