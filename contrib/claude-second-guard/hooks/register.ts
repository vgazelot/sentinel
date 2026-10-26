import type { Register } from 'claude-code'

import { check } from './policy'
import type { Config } from './policy'

// Loaded only in the sessions an unattended job starts (`--plugin-dir`), with --restricted and --permission-mode
// manual. It has the last word on every tool call: reading runs, the rest is refused — never asked, so no answer to a
// dialog (a switch to auto mode among them) can widen the session — and the engine's own refusal stands. What the
// session may not run, it writes down for you to run. Without it, the session is still manual: everything asks.
const list = (value: string | undefined) => (value ?? '').split(',').map(s => s.trim()).filter(Boolean)

export const register: Register = on => {
  const cfg: Config = { sshHosts: [], curlHosts: [] }

  on('session.start', async ($, e, next) => {
    cfg.sshHosts = list(await $.env.get('SECOND_SSH_HOSTS'))
    cfg.curlHosts = list(await $.env.get('SECOND_CURL_HOSTS'))
    $.ui.status('read-only: anything else refused, written down for you')
    return next(e)
  })

  on('tool.check', async ($, e, next) => {
    const engine = await next(e)
    if (engine.decision === 'deny') return engine
    const verdict = check(e.tool, e.input, cfg)
    return verdict.decision === 'allow'
      ? { decision: 'allow', reason: `${$.plugin.name}: ${verdict.reason}` }
      : { decision: 'deny', reason: `${$.plugin.name}: ${verdict.reason}. This session only reads: write it down as a command for the person to run.` }
  }).catch(($, e, next) => ({ decision: 'deny', reason: `${$.plugin.name}: its check failed, so it refuses` }))
}
