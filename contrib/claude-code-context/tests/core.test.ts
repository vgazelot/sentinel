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
  projectFromPrompt,
  projectFromTool,
  published,
  report,
  resumePrompt,
  settingsFrom,
  statusText,
  storyFromTool,
  usd,
} from '../hooks/core'
import shared from './published'

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

describe('published', () => {
  const base = { anchor: { project: null, story: 'sc-123' }, pinned: true, model: 'claude-opus-5-5', settings: DEFAULTS, handoff: '/handoff' }

  test('what the advisor would say, priced, for a launcher to show as is', async () => {
    const cold = published({ ...base, tokens: 366_000, lastResponseAt: 0, now: 2 * 3_600_000 })
    expect([cold.story, cold.pinned, cold.cold, cold.advise, usd(cold.nextUsd), usd(cold.freshUsd)]).toEqual(['sc-123', true, true, 'cold', '$2.93', '$0.58'])
    const big = published({ ...base, tokens: 400_000, lastResponseAt: 0, now: 60_000 })
    expect([big.cold, big.advise, usd(big.nextUsd)]).toEqual([false, 'handoff', '$0.08'])
    expect(published({ ...base, tokens: 120_000, lastResponseAt: 0, now: 2 * 3_600_000 }).advise).toBe('')
  })

  test('the file Sentinel reads: tests/published.ts, which its own tests read too', async () => {
    const p = published({ ...base, anchor: { project: 'mesh', story: 'sc-1234' }, tokens: 366_000, lastResponseAt: 1_000_000, now: 1_000_000 + 2 * 3_600_000 })
    expect(p).toEqual(shared)
  })

  test('no answer yet: the cache is not cold, nothing to advise', async () => {
    const p = published({ ...base, tokens: 200_000, lastResponseAt: null, now: 10 ** 12 })
    expect([p.cold, p.advise, p.answeredAt]).toEqual([false, '', null])
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

describe('linked project', () => {
  test('a typed /load-project or /resume-project names the project, with or without the plugin prefix', async () => {
    expect(projectFromPrompt('/load-project mesh fix the jwks rotation')).toBe('mesh')
    expect(projectFromPrompt('/project-memory:resume-project php-image')).toBe('php-image')
    expect(projectFromPrompt('/wrapup-project')).toBe(null)
    expect(projectFromPrompt('load-project mesh')).toBe(null)
  })

  test('so does the Skill tool when the model loads it', async () => {
    expect(projectFromTool('Skill', { skill: 'project-memory:load-project', args: 'mesh the jwks rotation' })).toBe('mesh')
    expect(projectFromTool('Skill', { skill: 'resume-project', args: ' run' })).toBe('run')
    expect(projectFromTool('Skill', { skill: 'project-memory:create-project', args: 'service-mesh' })).toBe(null)
    expect(projectFromTool('Skill', { skill: 'load-project' })).toBe(null)
    expect(projectFromTool('Bash', { command: '/load-project mesh' })).toBe(null)
  })
})

describe('status line', () => {
  test('a fresh session, then size, cold cache, quota, project and story', async () => {
    expect(statusText({ tokens: 0, cold: false, project: null, story: null, handoffAt: 350_000, handoff: '/handoff' })).toBe('fresh context')
    expect(statusText({ tokens: 208_000, cold: true, quota5h: 41.5, project: 'mesh', story: 'sc-1202', handoffAt: 350_000, handoff: '/handoff' }))
      .toBe('208k · cold · 5 h quota 42% · mesh · sc-1202')
  })

  test('past the threshold, a handoff is advised, or something to hand off to asked for', async () => {
    expect(statusText({ tokens: 400_000, cold: false, project: null, story: 'sc-1201', handoffAt: 350_000, handoff: '/handoff' })).toBe('400k · /handoff advised')
    expect(statusText({ tokens: 400_000, cold: false, project: 'mesh', story: null, handoffAt: 350_000, handoff: '/context-handoff' })).toBe(
      '400k · /context-handoff advised',
    )
    expect(statusText({ tokens: 400_000, cold: false, project: null, story: null, handoffAt: 350_000, handoff: '/handoff' })).toBe('400k · big context, nothing to hand off to')
  })
})

describe('texts', () => {
  test('the guard prices the send and offers to start over from the story, with or without a handoff', async () => {
    const text = guardText({ tokens: 366_000, model: 'claude-opus-5-5', idleMs: 92 * 60_000, project: 'mesh', story: 'sc-1205', settings: DEFAULTS, handoff: '/handoff' })
    expect(text).toContain('1 h 32')
    expect(text).toContain('≈ $2.93 just to read it again')
    expect(text).toContain('/fresh to start over from project mesh and sc-1205 if it is up to date (≈ $0.58)')
    expect(text).toContain('/handoff to write the state there first (≈ $2.93 then $0.58)')
  })

  test('with nothing linked, the guard offers /clear, /load-project and /ticket', async () => {
    const text = guardText({ tokens: 200_000, model: 'claude-opus-5-5', idleMs: 3 * 3_600_000, project: null, story: null, settings: DEFAULTS, handoff: '/handoff' })
    expect(text).toContain('/clear to start from scratch')
    expect(text).toContain('/load-project <name> or /ticket sc-12345')
    expect(text).not.toContain('/fresh')
  })

  test('a story handoff is written in English, ends on a checkable line, and stops on the wrong work', async () => {
    const text = handoffPrompt({ project: null, story: 'sc-1202' }, 380_000)
    expect(text.endsWith('HANDOFF-OK sc-1202')).toBe(true)
    expect(text).toContain('in English')
    expect(text).toContain('If sc-1202 is not what the current work is about')
    expect(text).not.toContain('handoff.md')
  })

  test('a project handoff syncs its memory and handoff.md without asking, pushes nothing, and confirms on the project', async () => {
    const text = handoffPrompt({ project: 'mesh', story: 'sc-1202' }, 380_000)
    expect(text).toContain('do the doc sync of /wrapup-project')
    expect(text).toContain('overwrite its handoff.md')
    expect(text).toContain('Ask nothing')
    expect(text).toContain('commit and push nothing')
    expect(text).toContain('Write in sc-1202, in English')
    expect(text.endsWith('HANDOFF-OK mesh')).toBe(true)
  })

  test('the fresh session resumes the project, then the story', async () => {
    expect(resumePrompt({ project: 'mesh', story: 'sc-1202' })).toBe(
      'Pick up project mesh and sc-1202: run the resume-project skill with "mesh" (it loads the project memory and its handoff.md); '
      + 'then read sc-1202 and its latest "Handoff" comment, check the linked branch and PR. Sum up in two lines where things stand, then carry on from the next step.')
    expect(resumePrompt({ project: null, story: 'sc-1202' })).toContain('Pick up sc-1202: read sc-1202')
  })

  test('the report gives size, cache, costs, quota and story', async () => {
    const text = report({
      tokens: 366_000, window: 1_000_000, model: 'claude-opus-5-5', cold: true, idleMs: 92 * 60_000, costUsd: 14.33,
      quota: { five_hour: 41, seven_day: 63 }, project: 'mesh', story: 'sc-1205', pinned: false, settings: DEFAULTS,
      handoff: '/handoff',
    })
    expect(text).toBe([
      'Context: 366k (37% of 1M) · cache cold for 1 h 32',
      'Next send ≈ $2.93 · fresh ≈ $0.58',
      'Session ≈ $14.33 · quota 5 h 41%, 7 d 63%',
      'Picks up from: project mesh and sc-1205 (detected) · /handoff writes the state there then starts fresh, /fresh starts fresh from it right away',
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
