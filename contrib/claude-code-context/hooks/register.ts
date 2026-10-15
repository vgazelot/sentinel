import type { EngineInterface as $T, Register } from 'claude-code'

import {
  compactContext,
  DEFAULTS,
  findStory,
  guardText,
  handoffPrompt,
  isCold,
  kTokens,
  report,
  resumePrompt,
  settingsFrom,
  statusText,
  storyFromTool,
  type Settings,
} from './core'

// A session's context, what it costs and when to hand off. The durable state of a piece of work lives
// in its story: a heavy session writes where it stands there, then a fresh session starts from it.
const TICK_MS = 30_000
const BYPASS_MS = 3 * 60_000

const ctx = {
  settings: DEFAULTS as Settings,
  story: null as string | null,
  pinned: false,
  lastResponseAt: null as number | null,
  bypassUntil: 0,
  handoff: null as string | null,
  resumeAfterClear: null as string | null,
  nudged: false,
}

async function figures($: $T) {
  const [usage, model, now] = await Promise.all([$.session.usage(), $.session.model(), $.clock.now()])
  const quota: Record<string, number> = {}
  for (const r of usage.rateLimits) quota[r.kind] = r.percentUsed
  return {
    usage,
    model,
    now,
    quota,
    tokens: usage.context.tokens ?? 0,
    cold: isCold(ctx.lastResponseAt, now, ctx.settings.ttlMin),
    idleMs: ctx.lastResponseAt === null ? null : now - ctx.lastResponseAt,
  }
}

async function refresh($: $T) {
  const f = await figures($)
  $.ui.status(statusText({ tokens: f.tokens, cold: f.cold, quota5h: f.quota.five_hour, story: ctx.story, handoffAt: ctx.settings.handoffAt }))
  return f
}

async function remember($: $T) {
  await $.store.set(`last:${await $.session.id()}`, ctx.lastResponseAt)
}

async function startHandoff($: $T) {
  const story = ctx.story
  if (!story) return
  const f = await figures($)
  ctx.handoff = story
  await $.prompt.submit({ text: handoffPrompt(story, f.tokens) })
}

async function resumeFresh($: $T, story: string) {
  ctx.resumeAfterClear = story
  $.clock.after(100, () => void $.command.run({ command: 'clear' }))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!e.isInteractive) return started

    ctx.settings = settingsFrom({
      CONTEXT_HANDOFF_TOKENS: await $.env.get('CONTEXT_HANDOFF_TOKENS'),
      CONTEXT_COLD_MIN_TOKENS: await $.env.get('CONTEXT_COLD_MIN_TOKENS'),
      CONTEXT_CACHE_TTL_MIN: await $.env.get('CONTEXT_CACHE_TTL_MIN'),
      CONTEXT_FRESH_TOKENS: await $.env.get('CONTEXT_FRESH_TOKENS'),
      CONTEXT_AUTO: await $.env.get('CONTEXT_AUTO'),
    })
    const saved = await $.store.get(`last:${await $.session.id()}`)
    if (typeof saved === 'number') ctx.lastResponseAt = saved

    await $.command.register({ name: 'budget', description: 'Session context: size, cache, cost, quota and linked ticket' })
    await $.command.register({ name: 'handoff', description: 'Writes the state into the linked story, then starts fresh from it' })
    await $.command.register({ name: 'fresh', description: 'Starts fresh from the linked story, without reading the current context again' })
    await $.command.register({ name: 'ticket', description: 'Links the session to a story (args: sc-12345), or shows the linked one' })

    $.clock.every(TICK_MS, () => void refresh($))
    void refresh($)
    return started
  })

  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if ((e.source === 'resume' || e.source === 'fork') && typeof e.seconds_since_last_response === 'number') {
      ctx.lastResponseAt = (await $.clock.now()) - e.seconds_since_last_response * 1000
    }
    if (e.source === 'clear') {
      const story = ctx.resumeAfterClear
      ctx.resumeAfterClear = null
      ctx.lastResponseAt = null
      ctx.bypassUntil = 0
      ctx.nudged = false
      if (story) return { ...result, initialUserMessage: resumePrompt(story) }
      ctx.story = null
      ctx.pinned = false
    }
    if (e.source === 'compact' && ctx.story) {
      ctx.nudged = false
      return { ...result, additionalContext: [...(result.additionalContext ?? []), compactContext(ctx.story)] }
    }
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind !== 'composer' || e.text.startsWith('/')) return next(e)
    const named = findStory(e.text)
    if (named && !ctx.pinned) ctx.story = named

    const f = await figures($)
    if (f.cold && f.idleMs !== null && f.tokens >= ctx.settings.coldMin && f.now > ctx.bypassUntil) {
      ctx.bypassUntil = f.now + BYPASS_MS
      const typed = e.text
      $.clock.after(50, () => void $.prompt.fill({ text: typed }))
      return { drop: guardText({ tokens: f.tokens, model: f.model, idleMs: f.idleMs, story: ctx.story, settings: ctx.settings }) }
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    if (!ctx.pinned && !e.agentId) {
      const story = storyFromTool(e.tool, e as unknown as Record<string, unknown>)
      if (story && story !== ctx.story) {
        ctx.story = story
        void refresh($)
      }
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId) return done
    ctx.lastResponseAt = await $.clock.now()
    void remember($)

    if (ctx.handoff) {
      const story = ctx.handoff
      ctx.handoff = null
      if (e.answer.includes(`HANDOFF-OK ${story}`)) {
        await resumeFresh($, story)
      } else {
        $.ui.toast(`Handoff not confirmed: check story ${story} before starting fresh.`, { timeoutMs: 15_000 })
      }
      return done
    }

    const f = await refresh($)
    if (!ctx.nudged && f.tokens >= ctx.settings.handoffAt) {
      ctx.nudged = true
      if (!ctx.story) {
        $.ui.toast(`${kTokens(f.tokens)} of context and no ticket linked: /ticket sc-12345 to be able to start fresh without losing anything.`, { timeoutMs: 15_000 })
      } else if (ctx.settings.auto && !e.answer.trim().endsWith('?')) {
        void startHandoff($)
      } else {
        await $.prompt.suggest({ text: '/handoff' })
        $.ui.toast(`${kTokens(f.tokens)} of context: /handoff writes the state into ${ctx.story} then starts fresh (Tab to take it).`, { timeoutMs: 15_000 })
      }
    }
    return done
  })

  on('session.end', async ($, e, next) => {
    await $.store.delete(`last:${await $.session.id()}`)
    return next(e)
  })

  on('command.run', { command: 'handoff' }, async $ => {
    if (!ctx.story) return { text: 'No ticket linked to this session: /ticket sc-12345, then /handoff.' }
    await startHandoff($)
    return { text: `Handing off: the state goes into ${ctx.story}, then a fresh session picks up from the story.` }
  })

  on('command.run', { command: 'fresh' }, async $ => {
    if (!ctx.story) return { text: 'No story linked to this session: /ticket sc-12345, then /fresh.' }
    await resumeFresh($, ctx.story)
    return { text: `Fresh session: it reads ${ctx.story} and carries on.` }
  })

  on('command.run', { command: 'ticket' }, async ($, e) => {
    const story = findStory(e.args)
    if (story) {
      ctx.story = story
      ctx.pinned = true
      void refresh($)
      return { text: `Session linked to ${story}.` }
    }
    return { text: ctx.story ? `Linked story: ${ctx.story}${ctx.pinned ? '' : ' (detected)'}.` : 'No story linked: /ticket sc-12345 to link one.' }
  })

  on('command.run', { command: 'budget' }, async $ => {
    const f = await refresh($)
    return {
      text: report({
        tokens: f.tokens,
        window: f.usage.context.window,
        model: f.model,
        cold: f.cold,
        idleMs: f.idleMs,
        costUsd: f.usage.cost?.usd,
        quota: f.quota,
        story: ctx.story,
        pinned: ctx.pinned,
        settings: ctx.settings,
      }),
    }
  })
}
