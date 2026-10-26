import { atom, read, update } from 'claude-code'
import type { EngineInterface as $T, Register } from 'claude-code'

import type { Summary, SummaryItem } from '../types'
import { band, DEEP_PROMPT, DEFAULT_URL, fromYou, isReview, prompt, statusLine, toasts } from './core'

// A window on Sentinel's own API: Sentinel polls GitHub and the status pages, owns the state and
// the notification policy; every session only reads GET /api/summary and posts the actions back.
// What began to need you shows above the prompt once a turn is over, never while Claude works; a
// session drawn on no surface with that band (VS Code alone) gets toasts instead.
const PANE = 'sentinel'
const POLL_MS = 20_000
const summaryAtom = atom({ plugin: 'sentinel', key: 'summary' } as const, null)
const errorAtom = atom({ plugin: 'sentinel', key: 'error' } as const, '')

// since: your last prompt here (ms), what the band counts from
const ctx = { url: DEFAULT_URL, seq: -1, deep: DEEP_PROMPT, since: 0 }

async function poll($: $T) {
  const since = ctx.seq < 0 ? '' : `?since=${ctx.seq}`
  try {
    const r = await $.http.fetch(`${ctx.url}/api/summary${since}`)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const summary = JSON.parse(r.text) as Summary
    // the first answer of a session is the baseline: no replay of what was already pushed
    const banded = (await $.session.surfaces()).some(s => s === 'terminal' || s === 'desktop')
    if (ctx.seq >= 0 && !banded) for (const line of toasts(summary.notifications)) $.ui.toast(line, { timeoutMs: 8000 })
    ctx.seq = summary.seq
    await update($, summaryAtom, () => summary)
    await update($, errorAtom, () => '')
    $.ui.status(statusLine(summary, ''))
  } catch (err) {
    await update($, errorAtom, () => String(err))
    $.ui.status(statusLine(null, String(err)))
  }
}

async function act($: $T, item: SummaryItem, path: string, body = '') {
  await $.http.fetch(`${ctx.url}/api/items/${item.id}/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  await poll($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!e.isInteractive) return started

    ctx.url = ((await $.env.get('SENTINEL_URL')) || DEFAULT_URL).replace(/\/$/, '')
    ctx.deep = (await $.env.get('SENTINEL_REVIEW_PROMPT')) || DEEP_PROMPT
    ctx.seq = -1
    ctx.since = await $.clock.now()
    await $.command.register({ name: 'sentinel', description: 'Sentinel: what waits on you (args: refresh)' })

    const tick = async () => {
      try {
        await poll($)
      } catch (err) {
        $.ui.log(`sentinel poll failed: ${String(err)}`, { to: 'debug' })
      }
    }
    void tick()
    $.clock.every(POLL_MS, tick)
    return started
  })

  on('prompt.submit', ($, e, next) => {
    // never in the way of the prompt
    if (fromYou(e.origin.kind)) void $.clock.now().then(now => { ctx.since = now }, () => undefined)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const summary = await read($, summaryAtom)
    if (!summary || e.props.isWorking || e.props.hasSurvey) return next(e)
    // read each time: a /clear gives the session another id
    const { lines, more, actionable } = band(summary, ctx.since, await $.session.id())
    if (!lines.length) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Text dimColor>Sentinel · meanwhile</Text>
        {lines.map((line, i) => <Text key={`line:${i}`} wrap="truncate-end">{line}</Text>)}
        {more || actionable ? (
          <Text dimColor wrap="truncate-end">{[more ? `+${more} more` : '', actionable ? '/sentinel to act on them' : ''].filter(Boolean).join(' · ')}</Text>
        ) : null}
      </Box>
    )
  })

  on('command.run', { command: 'sentinel' }, async $ => {
    await poll($)
    await $.ui.open({ id: PANE, title: 'Sentinel', closeOnEscape: true })
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const summary = await read($, summaryAtom)
    const error = await read($, errorAtom)
    const open = (url: string) => $.process.run(['open', url]).then(() => undefined)

    if (!summary) {
      return (
        <Box flexDirection="column">
          <Text color="error">{error ? `Sentinel is not answering on ${ctx.url} — ${error}` : 'Reading Sentinel…'}</Text>
          <Text dimColor>Set SENTINEL_URL if it does not run on {DEFAULT_URL}. Esc to close.</Text>
        </Box>
      )
    }
    const landing = summary.landing
    const row = (it: SummaryItem) => (
      <Box key={`item:${it.id}`} flexDirection="column" marginBottom={1}>
        <Text bold wrap="truncate-end">● {it.title}</Text>
        <Text dimColor wrap="truncate-end">
          {'  '}{it.ref}{it.why ? ` · ${it.why}` : ''}
        </Text>
        {it.claude ? <Text wrap="truncate-end">{'  '}Claude: {it.claude}</Text> : null}
        <Box flexDirection="row" gap={1}>
          <Button key={`open:${it.id}`} label="Open" onPress={() => open(it.url)} />
          <Button key={`done:${it.id}`} label="Done" onPress={() => act($, it, 'ack')} />
          <Button key={`later:${it.id}`} label="Later" onPress={() => act($, it, 'snooze', 'duration=tomorrow')} />
          {isReview(it.lane) ? (
            <Button key={`deep:${it.id}`} label="Devil's advocate"
              onPress={() => $.prompt.submit({ asUser: true, text: prompt(ctx.deep, it.url) }).then(() => undefined)} />
          ) : null}
        </Box>
      </Box>
    )

    return (
      <Box flexDirection="column">
        <Text dimColor wrap="truncate-end">
          {summary.attention} need{summary.attention === 1 ? 's' : ''} you · {summary.counts.deps ?? 0} bump(s) ·
          after approval: {landing.waiting ?? 0} waiting, {landing.checking ?? 0} checking, {landing.landed ?? 0} landed
          {landing.failed ? `, ${landing.failed} failed` : ''} · Esc to close
        </Text>
        <Box flexDirection="row" gap={1} marginBottom={1}>
          <Button key="web" label="Open Sentinel" onPress={() => open(ctx.url)} />
          <Button key="refresh" label="Refresh" onPress={() => poll($)} />
        </Box>
        {summary.items.length === 0 ? <Text dimColor>Nothing needs you. ✓</Text> : summary.items.map(row)}
      </Box>
    )
  })
}
