import { describe, expect, test } from 'claude-code/testing'

import {
  DEFAULTS,
  findStory,
  freshUsd,
  guardText,
  handoffPrompt,
  isCold,
  nextSendUsd,
  prices,
  report,
  settingsFrom,
  statusText,
  storyFromTool,
  usd,
} from '../hooks/core'

describe('cost', () => {
  test('reading a big context again is cheap, writing it again once the cache expired is not', async () => {
    expect(usd(nextSendUsd(618_000, 'claude-opus-5-5', false))).toBe('$0.12')
    expect(usd(nextSendUsd(366_000, 'claude-opus-5-5', true))).toBe('$2.93')
    expect(usd(freshUsd(DEFAULTS, 'claude-opus-5-5'))).toBe('$0.58')
  })

  test('model suffixes are ignored, an unknown model is priced as Opus 5.5', async () => {
    expect(prices('claude-opus-5-5[1m]')).toEqual(prices('claude-opus-5-5'))
    expect(prices('claude-opus-5').write1h).toBe(10)
    expect(prices('unknown-model')).toEqual(prices('claude-opus-5-5'))
  })

  test('the cache is cold past its lifetime, unknown without a known answer', async () => {
    expect(isCold(0, 61 * 60_000, 60)).toBe(true)
    expect(isCold(0, 59 * 60_000, 60)).toBe(false)
    expect(isCold(null, 10 ** 12, 60)).toBe(false)
  })
})

describe('linked story', () => {
  test('the last story named wins, whatever its spelling', async () => {
    expect(findStory('read sc-1201 then sc-1202')).toBe('sc-1202')
    expect(findStory('https://app.shortcut.com/acme/story/1203/certs')).toBe('sc-1203')
    expect(findStory('{"storyPublicId": 1204}')).toBe('sc-1204')
    expect(findStory('nothing to see')).toBe(null)
  })

  test('a Shortcut tool or a git command points at the story', async () => {
    expect(storyFromTool('mcp__shortcut__stories-get-by-id', { storyPublicId: 1201 })).toBe('sc-1201')
    expect(storyFromTool('Bash', { command: 'git switch -c feature/sc-1202-heap' })).toBe('sc-1202')
    expect(storyFromTool('Read', { file_path: '/tmp/sc-1234.md' })).toBe(null)
  })
})

describe('status line', () => {
  test('a fresh session, then size, cold cache, quota and story', async () => {
    expect(statusText({ tokens: 0, cold: false, story: null, handoffAt: 350_000 })).toBe('fresh context')
    expect(statusText({ tokens: 208_000, cold: true, quota5h: 41.5, story: 'sc-1202', handoffAt: 350_000 })).toBe('208k · cold · 5 h quota 42% · sc-1202')
  })

  test('past the threshold, a handoff is advised, or a ticket asked for', async () => {
    expect(statusText({ tokens: 400_000, cold: false, story: 'sc-1', handoffAt: 350_000 })).toBe('400k · /handoff advised')
    expect(statusText({ tokens: 400_000, cold: false, story: null, handoffAt: 350_000 })).toBe('400k · big context, no ticket')
  })
})

describe('texts', () => {
  test('the guard prices the send and offers to start over from the story, with or without a handoff', async () => {
    const text = guardText({ tokens: 366_000, model: 'claude-opus-5-5', idleMs: 92 * 60_000, story: 'sc-1205', settings: DEFAULTS })
    expect(text).toContain('1 h 32')
    expect(text).toContain('≈ $2.93 just to read it again')
    expect(text).toContain('/fresh to start over from sc-1205 if it is up to date (≈ $0.58)')
    expect(text).toContain('/handoff to write the state into it first (≈ $2.93 then $0.58)')
  })

  test('without a story, the guard offers /clear and /ticket', async () => {
    const text = guardText({ tokens: 200_000, model: 'claude-opus-5-5', idleMs: 3 * 3_600_000, story: null, settings: DEFAULTS })
    expect(text).toContain('/clear to start from scratch')
    expect(text).toContain('/ticket sc-12345')
    expect(text).not.toContain('/fresh')
  })

  test('the handoff is written in English, ends on a checkable line, and stops on the wrong story', async () => {
    const text = handoffPrompt('sc-1202', 380_000)
    expect(text.endsWith('HANDOFF-OK sc-1202')).toBe(true)
    expect(text).toContain('in English')
    expect(text).toContain('If sc-1202 is not the story of the current work')
  })

  test('the report gives size, cache, costs, quota and story', async () => {
    const text = report({
      tokens: 366_000, window: 1_000_000, model: 'claude-opus-5-5', cold: true, idleMs: 92 * 60_000, costUsd: 14.33,
      quota: { five_hour: 41, seven_day: 63 }, story: 'sc-1205', pinned: false, settings: DEFAULTS,
    })
    expect(text).toBe([
      'Context: 366k (37% of 1M) · cache cold for 1 h 32',
      'Next send ≈ $2.93 · fresh ≈ $0.58',
      'Session ≈ $14.33 · quota 5 h 41%, 7 d 63%',
      'Story: sc-1205 (detected) · /handoff writes the state into it then starts fresh, /fresh starts fresh from it right away',
    ].join('\n'))
  })
})

describe('settings', () => {
  test('environment variables replace the defaults, an invalid value is ignored', async () => {
    const s = settingsFrom({ CONTEXT_HANDOFF_TOKENS: '300000', CONTEXT_CACHE_TTL_MIN: 'x', CONTEXT_AUTO: '1' })
    expect(s.handoffAt).toBe(300_000)
    expect(s.ttlMin).toBe(DEFAULTS.ttlMin)
    expect(s.auto).toBe(true)
  })
})
