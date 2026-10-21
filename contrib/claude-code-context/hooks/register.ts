import type { EngineInterface as $T, Register } from 'claude-code'

import {
  compactContext,
  DEFAULTS,
  findStory,
  guardText,
  handoffPrompt,
  isCold,
  key,
  kTokens,
  projectFromPrompt,
  projectFromTool,
  published,
  report,
  resumePrompt,
  settingsFrom,
  statusText,
  storyFromTool,
  where,
  type Anchor,
  type Settings,
} from './core'

// A session's context, what it costs and when to hand off. The durable state of a piece of work lives
// in its story and, when the session loaded one, in its project's memory: a heavy session writes where
// it stands there, then a fresh session starts from it. What the status line shows is also written to
// <CLAUDE_CONFIG_DIR>/context-advisor/<session id>.json, for a launcher such as Sentinel to show.
const TICK_MS = 30_000
const BYPASS_MS = 3 * 60_000
const LINK = '/load-project <name> or /ticket sc-12345'
const COMMANDS = [
  ['budget', 'Session context: size, cache, cost, quota, linked project and ticket'],
  ['fresh', 'Starts fresh from the linked project / story, without reading the current context again'],
  ['ticket', 'Links the session to a story (args: sc-12345), or shows the linked one'],
] as const
const HANDOFF = 'Writes the state into the linked project memory / story, then starts fresh from it'
// /handoff, or this name when another plugin holds that one (a skill of the same name)
const HANDOFF_NAMES = ['handoff', 'context-handoff'] as const

const ctx = {
  settings: DEFAULTS as Settings,
  story: null as string | null,
  pinned: false,
  project: null as string | null,
  lastResponseAt: null as number | null,
  bypassUntil: 0,
  handoff: null as string | null,
  resumeAfterClear: null as Anchor | null,
  nudged: false,
  handoffCommand: '/handoff',
}

const anchor = (): Anchor => ({ project: ctx.project, story: ctx.story })
const anchored = () => Boolean(ctx.project || ctx.story)

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
  $.ui.status(
    statusText({ tokens: f.tokens, cold: f.cold, quota5h: f.quota.five_hour, ...anchor(), handoffAt: ctx.settings.handoffAt, handoff: ctx.handoffCommand }),
  )
  void publish($, f)
  return f
}

/** Where the session's state is published: one file per session id, read by a launcher such as Sentinel. */
async function publishedPath($: $T) {
  const home = (await $.env.get('CLAUDE_CONFIG_DIR')) || `${await $.env.get('HOME')}/.claude`
  return `${home}/context-advisor/${await $.session.id()}.json`
}

async function publish($: $T, f: Awaited<ReturnType<typeof figures>>) {
  try {
    const state = published({
      anchor: anchor(),
      pinned: ctx.pinned,
      tokens: f.tokens,
      model: f.model,
      lastResponseAt: ctx.lastResponseAt,
      now: f.now,
      settings: ctx.settings,
      handoff: ctx.handoffCommand,
    })
    await $.fs.write(await publishedPath($), JSON.stringify(state))
  } catch (err) {
    $.ui.log(`context: publish failed: ${String(err)}`, { to: 'debug' })
  }
}

/** A session gets back what it was linked to, from what was published for it; nothing published, nothing linked. Says whether anything was. */
async function restore($: $T) {
  let saved: Record<string, unknown> | null = null
  try {
    saved = JSON.parse(await $.fs.read(await publishedPath($)))
  } catch {
    // never published, or unreadable: the conversation starts unlinked
  }
  ctx.story = typeof saved?.story === 'string' ? saved.story : null
  ctx.project = typeof saved?.project === 'string' ? saved.project : null
  ctx.pinned = saved?.pinned === true
  return saved !== null
}

async function remember($: $T) {
  await $.store.set(`last:${await $.session.id()}`, ctx.lastResponseAt)
}

// submitted once the hook that decided it is over: a command.run or turn.complete hook holds the session
function startHandoff($: $T) {
  if (!anchored()) return
  ctx.handoff = key(anchor())
  $.clock.after(50, async () => {
    try {
      const f = await figures($)
      await $.prompt.submit({ text: handoffPrompt(anchor(), f.tokens) })
    } catch (err) {
      ctx.handoff = null
      $.ui.toast(`context: the handoff was not sent: ${String(err)}`, { timeoutMs: 15_000 })
    }
  })
}

async function resumeFresh($: $T, a: Anchor) {
  ctx.resumeAfterClear = a
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
    // a reload or a respawn starts the module over: the session keeps what it published;
    // nothing published yet, a launcher may have started it on a story (Sentinel's Pick up): linked as /ticket would
    const restored = await restore($)
    const preset = findStory((await $.env.get('CONTEXT_STORY')) ?? '')
    if (preset && !restored) {
      ctx.story = preset
      ctx.pinned = true
    }

    // a name another plugin holds is refused: the other commands, the status line and the guard still work
    const refused: string[] = []
    const take = async (name: string, description: string) => {
      try {
        await $.command.register({ name, description })
        return true
      } catch {
        refused.push(`/${name}`)
        return false
      }
    }
    for (const [name, description] of COMMANDS) await take(name, description)
    ctx.handoffCommand = ''
    for (const name of HANDOFF_NAMES) {
      if (await take(name, HANDOFF)) {
        ctx.handoffCommand = `/${name}`
        break
      }
    }
    if (refused.length) {
      const instead = ctx.handoffCommand && ctx.handoffCommand !== '/handoff' ? `; its handoff is ${ctx.handoffCommand}` : ''
      $.ui.toast(`context: ${refused.join(', ')} already belong to another plugin here${instead}.`, { timeoutMs: 15_000 })
    }

    $.clock.every(TICK_MS, () => void refresh($))
    void refresh($)
    return started
  })

  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if ((e.source === 'resume' || e.source === 'fork') && typeof e.seconds_since_last_response === 'number') {
      ctx.lastResponseAt = (await $.clock.now()) - e.seconds_since_last_response * 1000
    }
    // a fork carries on the same work; a resumed conversation may be another one
    if (e.source === 'resume') await restore($)
    if (e.source === 'clear') {
      const a = ctx.resumeAfterClear
      ctx.resumeAfterClear = null
      ctx.lastResponseAt = null
      ctx.bypassUntil = 0
      ctx.nudged = false
      if (!a) {
        ctx.story = null
        ctx.pinned = false
        ctx.project = null
      }
      void refresh($) // a new session id: published now, not at the next tick
      if (a) return { ...result, initialUserMessage: resumePrompt(a) }
    }
    if (e.source === 'resume' || e.source === 'fork') void refresh($)
    if (e.source === 'compact' && anchored()) {
      ctx.nudged = false
      return { ...result, additionalContext: [...(result.additionalContext ?? []), compactContext(anchor())] }
    }
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind !== 'composer') return next(e)
    const loaded = projectFromPrompt(e.text)
    if (loaded) {
      ctx.project = loaded
      void refresh($)
    }
    if (e.text.startsWith('/')) return next(e)
    const named = findStory(e.text)
    if (named && !ctx.pinned) ctx.story = named

    const f = await figures($)
    if (f.cold && f.idleMs !== null && f.tokens >= ctx.settings.coldMin && f.now > ctx.bypassUntil) {
      ctx.bypassUntil = f.now + BYPASS_MS
      const typed = e.text
      $.clock.after(50, () => void $.prompt.fill({ text: typed }))
      return { drop: guardText({ tokens: f.tokens, model: f.model, idleMs: f.idleMs, ...anchor(), settings: ctx.settings, handoff: ctx.handoffCommand }) }
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId) return result
    const args = e as unknown as Record<string, unknown>
    const project = projectFromTool(e.tool, args)
    const story = ctx.pinned ? null : storyFromTool(e.tool, args)
    if ((project && project !== ctx.project) || (story && story !== ctx.story)) {
      ctx.project = project ?? ctx.project
      ctx.story = story ?? ctx.story
      void refresh($)
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId) return done
    ctx.lastResponseAt = await $.clock.now()
    void remember($)

    if (ctx.handoff) {
      const confirm = ctx.handoff
      ctx.handoff = null
      if (e.answer.includes(`HANDOFF-OK ${confirm}`)) {
        await resumeFresh($, anchor())
      } else {
        $.ui.toast(`Handoff not confirmed: check ${where(anchor())} before starting fresh.`, { timeoutMs: 15_000 })
      }
      return done
    }

    const f = await refresh($)
    if (!ctx.nudged && f.tokens >= ctx.settings.handoffAt) {
      ctx.nudged = true
      if (!anchored()) {
        $.ui.toast(`${kTokens(f.tokens)} of context and nothing linked: ${LINK} to be able to start fresh without losing anything.`, { timeoutMs: 15_000 })
      } else if (ctx.settings.auto && !e.answer.trim().endsWith('?')) {
        startHandoff($)
      } else if (ctx.handoffCommand) {
        await $.prompt.suggest({ text: ctx.handoffCommand })
        $.ui.toast(`${kTokens(f.tokens)} of context: ${ctx.handoffCommand} writes the state into ${where(anchor())} then starts fresh (Tab to take it).`, { timeoutMs: 15_000 })
      }
    }
    return done
  })

  on('session.end', async ($, e, next) => {
    await $.store.delete(`last:${await $.session.id()}`)
    return next(e)
  })

  // under the name it got: a /handoff held by another plugin is that plugin's to run
  for (const command of HANDOFF_NAMES) {
    on('command.run', { command }, async ($, e, next) => {
      if (`/${e.command}` !== ctx.handoffCommand) return next(e)
      if (!anchored()) return { text: `Nothing linked to this session: ${LINK}, then ${ctx.handoffCommand}.` }
      startHandoff($)
      return { text: `Handing off: the state goes into ${where(anchor())}, then a fresh session picks up from there.` }
    })
  }

  on('command.run', { command: 'fresh' }, async $ => {
    if (!anchored()) return { text: `Nothing linked to this session: ${LINK}, then /fresh.` }
    await resumeFresh($, anchor())
    return { text: `Fresh session: it picks up ${where(anchor())} and carries on.` }
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
        ...anchor(),
        pinned: ctx.pinned,
        settings: ctx.settings,
        handoff: ctx.handoffCommand,
      }),
    }
  })
}
