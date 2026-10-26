import type { Register } from 'claude-code'

import { check } from './policy'
import type { Config } from './policy'

// Loaded only in the sessions an unattended job starts (`--plugin-dir`), with --restricted and --permission-mode
// manual: no allow rule or hook of yours widens them, and what this guard leaves to you asks. It has the last word on
// every tool call — reading runs, the rest waits for you — and never turns the engine's own refusal into a yes.
const list = (value: string | undefined) => (value ?? '').split(',').map(s => s.trim()).filter(Boolean)

export const register: Register = on => {
  const cfg: Config = { sshHosts: [], curlHosts: [] }

  on('session.start', async ($, e, next) => {
    cfg.sshHosts = list(await $.env.get('SECOND_SSH_HOSTS'))
    cfg.curlHosts = list(await $.env.get('SECOND_CURL_HOSTS'))
    $.ui.status('read-only: anything else asks you')
    return next(e)
  })

  on('tool.check', async ($, e, next) => {
    const engine = await next(e)
    if (engine.decision === 'deny') return engine
    const verdict = check(e.tool, e.input, cfg)
    return { decision: verdict.decision, reason: `${$.plugin.name}: ${verdict.reason}` }
  }).catch(($, e, next) => ({ decision: 'ask', reason: `${$.plugin.name}: its check failed, so it asks` }))
}
