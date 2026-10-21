import { describe, expect, test } from 'claude-code/testing'

// the test runner's timer, which the mod's own types leave out
declare const setTimeout: (callback: () => void, ms: number) => unknown

// The engine beneath the advisor: a session with an environment, a context and a file system to publish into.
const HOME = '/home/me'
const START = { cwd: '/work', surface: null, isInteractive: true }
const NOTHING_LINKED = 'No story linked: /ticket sc-12345 to link one.'
const MIN = 60_000

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

async function until(done: () => boolean) {
  for (let i = 0; i < 100 && !done(); i++) await sleep(5)
  expect(done()).toBe(true)
}

function engine(on: any, env: Record<string, string>, held: string[] = [], tokens = 90_000) {
  // one file system: the advisor reads back what it wrote; a /resume changes the session id
  const files = new Map<string, string>()
  const session = { id: 'session-1' }
  const path = () => `${HOME}/.claude/context-advisor/${session.id}.json`
  const toasts: string[] = []
  on('env.get', async (_: unknown, e: { name: string }) => ({ value: ({ HOME, ...env } as Record<string, string>)[e.name] }))
  on('store.get', async () => ({ value: undefined }))
  on('store.set', async () => ({ value: undefined }))
  on('session.id', async () => ({ value: session.id }))
  on('session.model', async () => ({ value: 'claude-opus-5-5' }))
  on('session.usage', async () => ({ value: { startedAt: 0, context: { tokens, window: 1_000_000 }, rateLimits: [] } }))
  const clock = { now: 1_000 }
  on('clock.now', async () => ({ value: clock.now }))
  // the status line's period passes when the test says so, a delay as it would
  let period = () => {}
  on('clock.every', () => new Promise(r => (period = () => r({ value: undefined }))))
  on('clock.after', async (_: unknown, e: { ms: number }) => {
    await sleep(e.ms)
    return { value: undefined }
  })
  const tick = async () => {
    period()
    await sleep(20)
  }
  // what the advisor submits, puts in the prompt box and runs; the box holds `box.text`
  const submitted: string[] = []
  const filled: string[] = []
  const ran: string[] = []
  const box = { text: '' }
  on('prompt.submit', async (_: unknown, e: { text: string }) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('prompt.read', async () => ({ value: { text: box.text, cursor: 0 } }))
  on('prompt.fill', async (_: unknown, e: { text: string }) => {
    filled.push(e.text)
    return { isFilled: true }
  })
  // a command the advisor leaves to the engine: `held` ones are another plugin's
  on('command.run', async (_: unknown, e: { command: string }) => {
    ran.push(e.command)
    return { text: `the other plugin's /${e.command}` }
  })
  on('turn.start', async (_: unknown, e: { turnId: string }) => ({ turnId: e.turnId }))
  on('turn.complete', async (_: unknown, e: { answer: string }) => ({ text: e.answer }))
  on('classic.Stop', async () => ({}))
  on('ui.status', async () => ({ value: undefined }))
  on('ui.toast', async (_: unknown, e: { text: string }) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.log', async () => ({ value: undefined }))
  on('fs.write', async (_: unknown, e: { path: string; text: string }) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', async (_: unknown, e: { path: string }) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error('ENOENT')
    return { value: text }
  })
  on('command.register', async (_: unknown, e: { name: string }) => {
    if (held.includes(e.name)) throw new Error(`"/${e.name}" refused: it is the plugin's /other:${e.name}`)
    return { value: undefined }
  })
  on('session.start', async (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('classic.SessionStart', async () => ({}))
  // the advisor publishes without waiting: the test waits for the session's file to say what it expects
  const state = async (until: (s: Record<string, unknown>) => boolean) => {
    for (let i = 0; i < 100; i++) {
      const text = files.get(path())
      if (text && until(JSON.parse(text))) return JSON.parse(text) as Record<string, unknown>
      await new Promise<void>(r => setTimeout(r, 5))
    }
    throw new Error(`never published as expected: ${files.get(path())}`)
  }
  return { state, toasts, session, clock, tick, submitted, filled, ran, box }
}

// what /ticket says the session is linked to
const linked = async ($: any) => (await $.command.run({ command: 'ticket', args: '' })).text

describe('publishing', () => {
  test('a story given at start (CONTEXT_STORY) is linked as /ticket would, and published', async ($, on) => {
    const { state } = engine(on, { CONTEXT_STORY: 'sc-4242' })
    await $.session.start(START)
    const s = await state(s => s.story === 'sc-4242')
    expect([s.pinned, s.tokens, s.handoff]).toEqual([true, 90_000, '/handoff'])
  })

  test('/handoff held by another plugin: the advisor hands off as /context-handoff, says so, publishes it and leaves /handoff to it', async ($, on) => {
    const { state, toasts } = engine(on, {}, ['handoff'])
    await $.session.start(START)
    await state(s => s.handoff === '/context-handoff')
    expect(toasts.join(' ')).toContain('/handoff already belong to another plugin here; its handoff is /context-handoff')
    expect((await $.command.run({ command: 'handoff', args: '' })).text).toBe("the other plugin's /handoff")
    expect((await $.command.run({ command: 'context-handoff', args: '' })).text).toContain('then /context-handoff.')
  })

  test('a resumed conversation gets back the story it published, one that published nothing starts unlinked', async ($, on) => {
    const { state, session } = engine(on, {})
    await $.session.start(START)
    await state(s => s.story === null)
    await $.command.run({ command: 'ticket', args: 'sc-7777' })
    await state(s => s.story === 'sc-7777')
    session.id = 'session-2'
    await $.classic.SessionStart({ source: 'resume' })
    expect(await linked($)).toBe(NOTHING_LINKED)
    await state(s => s.story === null)
    session.id = 'session-1'
    await $.classic.SessionStart({ source: 'resume' })
    expect(await linked($)).toBe('Linked story: sc-7777.')
  })

  test('the mod started again (a reload, a respawn) keeps the story the session published, over CONTEXT_STORY', async ($, on) => {
    const { state } = engine(on, { CONTEXT_STORY: 'sc-4242' })
    await $.session.start(START)
    await state(s => s.story === 'sc-4242')
    await $.command.run({ command: 'ticket', args: 'sc-5555' })
    await state(s => s.story === 'sc-5555')
    await $.session.start(START)
    expect(await linked($)).toBe('Linked story: sc-5555.')
  })
})

describe('handoff', () => {
  test('/context-handoff answers, submits the handoff once its own hook is over, then starts fresh from the story', async ($, on) => {
    const { submitted, ran } = engine(on, { CONTEXT_STORY: 'sc-4242' }, ['handoff'])
    await $.session.start(START)
    expect((await $.command.run({ command: 'context-handoff', args: '' })).text).toContain('Handing off')
    await until(() => submitted.length === 1)
    expect(submitted[0]).toContain('HANDOFF-OK sc-4242')
    await $.turn.complete({ answer: 'Written.\nHANDOFF-OK sc-4242', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' })
    await until(() => submitted.length === 2)
    expect([ran, submitted[1]?.startsWith('Pick up sc-4242')]).toEqual([['clear'], true])
  })
})
