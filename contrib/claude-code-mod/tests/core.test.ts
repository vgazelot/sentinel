import { describe, expect, test } from 'claude-code/testing'

import { band, fromYou, isReview, prompt, statusLine, toasts } from '../hooks/core'
import type { Summary, SummaryItem } from '../types'

const summary = (over: Partial<Summary> = {}): Summary => ({
  attention: 0,
  counts: {},
  landing: {},
  items: [],
  answered: [],
  notifications: [],
  seq: 0,
  agents: { running: 0, queued: 0 },
  base_url: 'http://localhost:8300/',
  ...over,
})

describe('status line', () => {
  test('calm', async () => {
    expect(statusLine(summary(), '')).toBe('Sentinel ✓ nothing for you')
  })

  test('what waits on you first, then breakage, bumps and Claude', async () => {
    const s = summary({ attention: 3, counts: { broken: 1, deps: 2 }, agents: { running: 1, queued: 0 } })
    expect(statusLine(s, '')).toBe('Sentinel ● 3 for you · ⚠ 1 broke after merge · 2 bumps · Claude reading')
  })

  test('offline', async () => {
    expect(statusLine(null, 'connection refused')).toBe('Sentinel offline')
    expect(statusLine(null, '')).toBe('Sentinel …')
  })
})

describe('toasts', () => {
  const note = (id: number) => ({ id, title: `t${id}`, body: id % 2 ? 'why' : null, url: 'u', reason: 'new', lane: 'you' })

  test('a burst becomes three toasts and a count', async () => {
    expect(toasts([1, 2, 3, 4, 5].map(note))).toEqual(['t1 — why', 't2', 't3 — why', 'and 2 more in Sentinel'])
  })

  test('nothing new, nothing shown', async () => {
    expect(toasts([])).toEqual([])
  })
})

describe('band above the prompt', () => {
  const item = (id: number, at: string, over: Partial<SummaryItem> = {}): SummaryItem => ({
    id, lane: 'you', ref: `r#${id}`, title: `t${id}`, url: 'u', why: '', since: at, subject: '', at, claude: '', ...over,
  })
  const since = Date.parse('2026-10-26T14:00:00Z')

  test('only what began to need you after your last prompt here', async () => {
    const items = [item(1, '2026-10-26T13:59:59Z'), item(2, '2026-10-26T14:03:00Z', { why: 'changes requested' })]
    expect(band(summary({ items }), since, 'me')).toEqual({ lines: ['r#2 · t2 — changes requested'], more: 0, actionable: true })
  })

  test('this session waiting is not news to itself, another one is', async () => {
    const items = [
      item(1, '2026-10-26T14:05:00Z', { lane: 'session', subject: 'me', why: 'waits for you' }),
      item(2, '2026-10-26T14:05:00Z', { lane: 'session', subject: 'other', ref: 'sc-12345', title: 'Heap', why: 'waits for you' }),
    ]
    expect(band(summary({ items }), since, 'me').lines).toEqual(['sc-12345 · Heap — waits for you'])
  })

  test('then the other sessions that answered meanwhile', async () => {
    const answered = [
      { subject: 'me', ref: 'dev-7d', title: 'Mine', url: 'u', at: '2026-10-26T14:06:00Z' },
      { subject: 'b', ref: 'dev-b2', title: 'Old', url: 'u', at: '2026-10-26T13:50:00Z' },
      { subject: 'c', ref: 'sc-23456', title: 'Monitors', url: 'u', at: '2026-10-26T14:06:00Z' },
    ]
    const items = [item(1, '2026-10-26T14:01:00Z')]
    expect(band(summary({ items, answered }), since, 'me').lines).toEqual(['r#1 · t1', 'sc-23456 · Monitors — answered'])
    expect(band(summary({ answered }), since, 'me').actionable).toBe(false)
  })

  test('a few lines, then a count', async () => {
    const items = [1, 2, 3, 4, 5].map(id => item(id, '2026-10-26T14:10:00Z'))
    expect(band(summary({ items }), since, 'me')).toEqual({ lines: ['r#1 · t1', 'r#2 · t2', 'r#3 · t3'], more: 2, actionable: true })
  })

  test('an older Sentinel without the sessions that answered', async () => {
    const older = { ...summary({ items: [item(1, '2026-10-26T14:01:00Z')] }), answered: undefined } as unknown as Summary
    expect(band(older, since, 'me').lines).toEqual(['r#1 · t1'])
  })

  test('a date it cannot read is never news', async () => {
    expect(band(summary({ items: [item(1, '')] }), since, 'me').lines).toEqual([])
  })

  test('only your own prompts move it, not what the session gets on its own', async () => {
    expect(['composer', 'bridge'].map(fromYou)).toEqual([true, true])
    expect(['task-notification', 'peer', 'scheduled-trigger', 'auto-continuation', 'sdk'].map(fromYou)).toEqual([false, false, false, false, false])
  })
})

describe('reviews in the session', () => {
  test('prompt template', async () => {
    expect(prompt('/my-review {url} read-only', 'https://github.com/o/r/pull/1')).toBe('/my-review https://github.com/o/r/pull/1 read-only')
  })

  test('only review requests get the review buttons', async () => {
    expect(isReview('team')).toBe(true)
    expect(isReview('followup')).toBe(false)
  })
})
