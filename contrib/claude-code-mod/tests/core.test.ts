import { describe, expect, test } from 'claude-code/testing'

import { isReview, prompt, statusLine, toasts } from '../hooks/core'
import type { Summary } from '../types'

const summary = (over: Partial<Summary> = {}): Summary => ({
  attention: 0,
  counts: {},
  landing: {},
  items: [],
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

describe('reviews in the session', () => {
  test('prompt template', async () => {
    expect(prompt('/my-review {url} read-only', 'https://github.com/o/r/pull/1')).toBe('/my-review https://github.com/o/r/pull/1 read-only')
  })

  test('only review requests get the review buttons', async () => {
    expect(isReview('team')).toBe(true)
    expect(isReview('followup')).toBe(false)
  })
})
