import type { Register } from 'claude-code'

import { check } from './policy'
import type { Config } from './policy'

// Loaded only in the sessions an unattended job starts (`--plugin-dir`), with --restricted and --permission-mode
// manual. It has the last word on every tool call: reading runs; the rest is refused while nobody is there — never
// asked, so no dialog waits for an answer nobody gives (or a click on "switch to auto mode" that hands the asks to a
// classifier) — and asks you once you wrote in the session: a prompt after the job's own is yours. The engine's own
// refusal stands. Without the guard, the session is still manual: everything asks. SECOND_WORKTREE (a fix on a pull
// request) adds the files of that checkout: edited and committed there, never pushed.
const list = (value: string | undefined) => (value ?? '').split(',').map(s => s.trim()).filter(Boolean)

export const register: Register = on => {
  const cfg: Config = { sshHosts: [], curlHosts: [], worktree: '' }

  on('session.start', async ($, e, next) => {
    cfg.sshHosts = list(await $.env.get('SECOND_SSH_HOSTS'))
    cfg.curlHosts = list(await $.env.get('SECOND_CURL_HOSTS'))
    const worktree = (await $.env.get('SECOND_WORKTREE'))?.trim() ?? ''
    cfg.worktree = worktree.startsWith('/') ? worktree : ''
    $.ui.status(cfg.worktree ? 'edits and commits in its worktree, reads elsewhere; anything else refused — asked once you write in it'
      : 'reads on its own; anything else refused — asked once you write in it')
    return next(e)
  })

  on('tool.check', async ($, e, next) => {
    const engine = await next(e)
    if (engine.decision === 'deny') return engine
    const verdict = check(e.tool, e.input, cfg)
    if (verdict.decision === 'allow') return { decision: 'allow', reason: `${$.plugin.name}: ${verdict.reason}` }
    // the job's prompt is the first; one more is the person, there to answer (the engine counts, reloads or not)
    return (await $.session.turns()) >= 2
      ? { decision: 'ask', reason: `${$.plugin.name}: ${verdict.reason}` }
      : { decision: 'deny', reason: `${$.plugin.name}: ${verdict.reason}. ${cfg.worktree
        ? 'This session edits and commits in its worktree, nothing else: write the rest down as a command for the person to run.'
        : 'This session only reads: write it down as a command for the person to run.'}` }
  }).catch(($, e, next) => ({ decision: 'deny', reason: `${$.plugin.name}: its check failed, so it refuses` }))
}
