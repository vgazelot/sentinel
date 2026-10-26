/** What an unattended session may run on its own: reading. The lists name what reads, never what writes — an unknown
 *  tool, command, subcommand or flag asks the person, and so does anything this reader does not parse (command
 *  substitution, a heredoc, a subshell). It narrows what runs without you; it is not a sandbox. */

export type Verdict = { decision: 'allow' | 'ask'; reason: string }
/** Host patterns (`*` within a name) ssh and curl may reach; localhost is always readable by curl. */
export type Config = { sshHosts: string[]; curlHosts: string[] }

const allow = (reason: string): Verdict => ({ decision: 'allow', reason })
const ask = (reason: string): Verdict => ({ decision: 'ask', reason })

const READ_TOOLS = new Set([
  'Read', 'Grep', 'Glob', 'LS', 'NotebookRead', 'WebSearch', 'ToolSearch', 'Skill', 'Agent', 'Task', 'TodoWrite',
  'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet', 'AskUserQuestion', 'ListMcpResourcesTool', 'ReadMcpResourceTool',
])
// keys, credentials, environment files, the tool configs that hold tokens: reading them is how a steered session would
// leak them
const SECRET = new RegExp([
  String.raw`(^|/)\.env($|\.)`, String.raw`\.(pem|key|p12|pfx|kdbx|keychain-db)$`, String.raw`(^|/)id_(rsa|ed25519|ecdsa|dsa)`,
  String.raw`(^|/)\.(ssh|aws|gnupg|config|kube|docker|azure|gcloud|terraform\.d|vault-token|password-store)(/|$)`,
  String.raw`(^|/)\.(netrc|pgpass|git-credentials|npmrc|pypirc|terraformrc|claude\.json|mcp\.json)$`, String.raw`(^|/)\.claude/`,
  String.raw`_history$`, String.raw`(^|/)environ$`, String.raw`/Keychains/`, 'credential', 'secret', 'passw', 'token', 'vault_pass',
].join('|'), 'i')
// …but where ssh goes is no secret: host names, users, bastions — what a diagnosis needs first
const SSH_MAP = /(^|\/)\.ssh\/(config|known_hosts)$/
const secretPath = (w: string) => SECRET.test(w) && !SSH_MAP.test(w)
// a glob or a brace in a path could name one of those without spelling it
const DODGE = /[*?[{]/

export function check(tool: string, input: unknown, cfg: Config): Verdict {
  const args = (input ?? {}) as Record<string, unknown>
  if (READ_TOOLS.has(tool)) {
    const path = [args.file_path, args.path].find(v => typeof v === 'string' && secretPath(v))
      ?? (typeof args.pattern === 'string' && tool === 'Glob' && secretPath(args.pattern) ? args.pattern : undefined)
    return path ? ask(`${tool} on ${path}: secrets are read with you`) : allow(`${tool} reads`)
  }
  if (tool === 'Bash') return typeof args.command === 'string' ? shell(args.command, cfg, 0) : ask('Bash without a command')
  if (tool.startsWith('mcp__')) return mcp(tool)
  return ask(`${tool} is not a reading tool`)
}

// --- MCP: by the verbs in the tool's name ------------------------------------------------------------------------
// not `query`: a tool named for its query language alone may write as well as read
const READ_VERB = /^(get|list|search|read|describe|aggregate|find|ask|resolve|fetch|history|schema|sample|metadata|info|view|show|count|lookup)$/
const WRITE_VERB = /^(create|update|delete|add|remove|set|send|post|reply|merge|upload|execute|exec|run|activate|deactivate|assign|unassign|change|push|fork|trash|untrash|label|unlabel|mark|unmark|apply|schedule|draft|forward|respond|edit|rename|move|close|approve|dismiss|publish|write|invite|archive|start|stop|restart|cancel|enable|disable|subscribe|authenticate|complete|click|type|fill|drag|drop|press|navigate|evaluate|install|import|export|batch|insert|modify|patch|put|submit|trigger|comment|react|pin|unpin|share|sync|notify|analyze|handle|emulate|resize|tabs|upload)$/

function mcp(tool: string): Verdict {
  const name = tool.split('__').slice(2).join('_')
  const verbs = name.replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase().split(/[-_]+/)
  if (verbs.some(v => WRITE_VERB.test(v))) return ask(`${tool} may change something`)
  return verbs.some(v => READ_VERB.test(v)) ? allow(`${tool} reads`) : ask(`${tool} is not a reading tool`)
}

// --- Shell: a small reader, then each command against the list --------------------------------------------------
type Redirect = { op: string; target: string }
type Segment = { words: string[]; redirects: Redirect[]; expands: string[] }
// what a command may expand from the environment; any other variable may hold a token
const SAFE_VARS = new Set(['HOME', 'USER', 'LOGNAME', 'PWD', 'OLDPWD', 'PATH', 'SHELL', 'TERM', 'LANG', 'TMPDIR', 'HOSTNAME', '?'])

/** Commands split on `|`, `&&`, `||`, `;`, `&` and new lines, with their redirections; null when it holds what this
 *  reader leaves to the person. */
export function parse(src: string): Segment[] | null {
  const segments: Segment[] = [{ words: [], redirects: [], expands: [] }]
  let word: string | null = null
  let i = 0
  const seg = () => segments[segments.length - 1]
  const end = () => {
    if (word !== null) seg().words.push(word)
    word = null
  }
  while (i < src.length) {
    const c = src[i]
    if (c === '\\') {
      if (i + 1 < src.length && src[i + 1] !== '\n') word = (word ?? '') + src[i + 1]
      i += 2
    } else if (c === "'") {
      const close = src.indexOf("'", i + 1)
      if (close < 0) return null
      word = (word ?? '') + src.slice(i + 1, close)
      i = close + 1
    } else if (c === '"') {
      let out = ''
      i += 1
      for (; i < src.length && src[i] !== '"'; i++) {
        if (src[i] === '`' || (src[i] === '$' && src[i + 1] === '(')) return null
        if (src[i] === '$') seg().expands.push(variable(src, i))
        if (src[i] === '\\' && '"\\$`'.includes(src[i + 1] ?? '')) i += 1
        out += src[i]
      }
      if (i >= src.length) return null
      word = (word ?? '') + out
      i += 1
    } else if (c === '`' || (c === '$' && '(\'"'.includes(src[i + 1] ?? '')) || ((c === '<' || c === '>') && src[i + 1] === '(')) {
      // $( ), backticks, <( ) run commands; $'…' decodes escapes the names below would not see
      return null
    } else if (c === '(' || c === ')') {
      return null
    } else if (c === '#' && word === null) {
      const nl = src.indexOf('\n', i)
      i = nl < 0 ? src.length : nl
    } else if (c === ' ' || c === '\t') {
      end()
      i += 1
    } else if (c === '\n' || c === ';' || c === '|' || c === '&') {
      if (c === '&' && src[i + 1] === '>') {
        end()
        const r = redirect(src, i + 2)
        if (!r) return null
        seg().redirects.push({ op: src[i + 2] === '>' ? '&>>' : '&>', target: r.target })
        i = r.next
        continue
      }
      if (c === '&' && /\d$/.test(word ?? '') === false && src[i - 1] === '>') return null
      end()
      if (seg().words.length || seg().redirects.length) segments.push({ words: [], redirects: [], expands: [] })
      i += src[i + 1] === c || (c === '|' && src[i + 1] === '&') ? 2 : 1
    } else if (c === '>' || c === '<') {
      const fd = word !== null && /^\d$/.test(word) ? word : ''
      if (!fd) end()
      word = null
      let op = c
      let j = i + 1
      while (j < src.length && (src[j] === '>' || src[j] === '<' || src[j] === '&' || src[j] === '|') && op.length < 3) {
        op += src[j]
        j += 1
      }
      if (op.startsWith('<<')) return null // a heredoc's body reads as commands
      const r = redirect(src, j)
      if (!r) return null
      seg().redirects.push({ op: fd + op, target: r.target })
      i = r.next
    } else {
      if (c === '$') seg().expands.push(variable(src, i))
      word = (word ?? '') + c
      i += 1
    }
  }
  end()
  return segments.filter(s => s.words.length || s.redirects.length)
}

/** The name after a `$`: `$NAME`, `${NAME…}`, `$1`, `$?`; '' for a lone `$`. */
function variable(src: string, at: number): string {
  const m = /^\$(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*)|([0-9?#@*!$-]))/.exec(src.slice(at))
  return m ? (m[1] ?? m[2] ?? m[3] ?? '') : ''
}

function redirect(src: string, from: number): { target: string; next: number } | null {
  let j = from
  while (src[j] === ' ' || src[j] === '\t') j += 1
  const parsed = parse(src.slice(j).split(/[\s;|&]/)[0] ?? '')
  const target = parsed?.[0]?.words[0]
  if (!target) return null
  let k = j
  while (k < src.length && !/[\s;|&]/.test(src[k])) k += 1
  return { target, next: k }
}

const MAX_DEPTH = 3

function shell(src: string, cfg: Config, depth: number): Verdict {
  if (depth > MAX_DEPTH) return ask('commands nested too deep')
  const segments = parse(src)
  if (!segments) return ask('a command substitution, subshell, heredoc or unclosed quote: read by you')
  if (!segments.length) return allow('nothing to run')
  for (const s of segments) {
    const variable = s.expands.find(v => v && !SAFE_VARS.has(v))
    if (variable) return ask(`expands $${variable}: a variable may hold a token`)
    for (const r of s.redirects) {
      const out = r.op.replace(/^\d/, '')
      if (out === '<' && !secretPath(r.target)) continue
      if (out === '>&' && /^(\d|-)$/.test(r.target)) continue
      if (['>', '>>', '&>', '&>>', '>|'].includes(out) && r.target === '/dev/null') continue
      return ask(`writes to ${r.target}`)
    }
    const v = command(s.words, cfg, depth)
    if (v.decision === 'ask') return v
  }
  return allow(`reads: ${src.length > 80 ? `${src.slice(0, 77)}…` : src}`)
}

const PLAIN = new Set([
  'cat', 'ls', 'head', 'tail', 'wc', 'zcat', 'cut', 'tr', 'column', 'nl',
  'diff', 'cmp', 'comm', 'basename', 'dirname', 'realpath', 'readlink', 'stat', 'file', 'md5sum', 'sha1sum', 'sha256sum',
  'shasum', 'pgrep', 'uptime', 'free', 'df', 'du', 'vmstat', 'iostat', 'mpstat', 'lsof', 'ss', 'netstat', 'uname',
  'id', 'whoami', 'groups', 'nproc', 'lscpu', 'lsblk', 'lsmem', 'w', 'who', 'last', 'echo', 'printf', 'true', 'false',
  'test', '[', 'sleep', 'which', 'type', 'pwd', 'cd', 'dig', 'host', 'nslookup', 'getent', 'tac', 'rev', 'fold',
  'expand', 'seq', 'cal', 'strings', 'locale',
])

function command(words: string[], cfg: Config, depth: number): Verdict {
  if (!words.length) return allow('nothing to run')
  if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0])) return ask(`sets ${words[0].split('=')[0]} for the command`)
  const name = words[0].split('/').pop() ?? ''
  const rest = words.slice(1)
  // curl's client certificate and key are used, never printed (mutual TLS to a monitoring front)
  const cert = (w: string, k: number) => name === 'curl' && (/^(-E|--cert|--key|--cacert)$/.test(rest[k - 1] ?? '') || /^--(cert|key|cacert)=/.test(w))
  // a path starting at ~, . or a variable, or through a hidden directory, with a glob or a brace in it
  const dodges = (w: string) => DODGE.test(w) && /^[~$]|(^|\/)\.[^/]*\/|\/[.$]/.test(w)
  const secret = rest.find((w, k) => (secretPath(w) || dodges(w)) && !cert(w, k))
  if (secret) return ask(`reads ${secret}: secrets are read with you`)
  const no = (why: string) => ask(`${name}: ${why}`)
  const yes = () => allow(name)
  const has = (...flags: string[]) => rest.some(w => flags.some(f => w === f || w.startsWith(`${f}=`)))
  const positional = rest.filter(w => !w.startsWith('-'))

  if (PLAIN.has(name)) return yes()
  switch (name) {
    case 'sort':
      return has('-o', '--output', '--compress-program') || rest.some(w => /^-[a-zA-Z]*o/.test(w)) ? no('writes a file or runs a program') : yes()
    case 'base64':  // -o writes a file (macOS)
      return has('-o', '--output') || rest.some(w => /^-[a-zA-Z]*o/.test(w)) ? no('writes its output to a file') : yes()
    case 'jq':  // env and $ENV read the environment, input(s) and the file flags read other files
      return rest.some(w => /\benv\b|\$ENV|\binputs?\b|\$__loc__|\bimport\b|\binclude\b/.test(w))
        || has('--rawfile', '--slurpfile', '-f', '--from-file', '-L', '--library-path') ? no('reads the environment or other files') : yes()
    case 'grep': case 'egrep': case 'fgrep': case 'zgrep':  // a recursive grep reads every file, hidden ones too: rg skips them
      return has('-r', '-R', '--recursive', '-d', '--directories') || rest.some(w => /^-[a-zA-Z]*[rR]/.test(w)) || everywhere(rest)
        ? no('recursive: rg reads the same without hidden files') : yes()
    case 'uniq':
      return positional.length > 1 ? no('writes its output to a file') : yes()
    case 'rg':
      return has('--pre') ? no('--pre runs a command')
        : has('--hidden', '--no-ignore', '-u', '-uu', '-uuu', '-.', '--unrestricted') || everywhere(rest) ? no('hidden files, or everywhere') : yes()
    case 'find':
      return has('-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprint0', '-fprintf', '-fls') ? no('acts on what it finds') : yes()
    case 'sed': {
      const flags = rest.filter((w, k) => w.startsWith('-') && rest[k - 1] !== '-e' && rest[k - 1] !== '--expression')
      if (!flags.every(w => /^-[nEruzs]+$|^-e$|^--(expression|quiet|silent|regexp-extended)$/.test(w))) return no('edits in place or reads a script file')
      const scripts = rest.filter((w, k) => rest[k - 1] === '-e' || rest[k - 1] === '--expression')
      return (scripts.length ? scripts : positional.slice(0, 1)).every(s => SED_SAFE.test(s)) ? yes() : no('a script that may read, write or run')
    }
    case 'awk': case 'gawk': case 'mawk': {
      if (rest.some(w => /^(-f|--file|-l|--load|-i|--include|-E|--exec)(=|$)/.test(w))) return no('a program file or an extension')
      // every fragment of the program, -e ones included: one is enough to run a command
      return rest.some((w, k) => rest[k - 1] !== '-F' && /system|getline|\||>|ENVIRON|PROCINFO|close\s*\(|fflush|@load|@include/.test(w))
        ? no('its program writes, runs commands or reads the environment') : yes()
    }
    case 'ps':  // `e` (BSD style) and -E print every process's environment; `ps aux` reads the same without it
      return rest.some(w => /^-?[a-zA-Z]+$/.test(w) && /[eE]/.test(w)) ? no('e may print the environment of processes') : yes()
    case 'top':
      return has('-b', '-l', '-n') || rest.some(w => /^-[a-zA-Z]*b/.test(w)) ? yes() : no('interactive without -b')
    case 'ping':
      return has('-c') || rest.some(w => /^-c\d+$/.test(w)) ? yes() : no('runs forever without -c')
    case 'hostname':
      return positional.length ? no('sets the host name') : yes()
    case 'date':
      return has('-s', '--set') || positional.some(w => !w.startsWith('+')) ? no('sets the clock') : yes()
    case 'ip':
      return rest.some(w => /^(add|del|delete|change|replace|set|flush|append|prepend|exec|save|restore)$/.test(w)) ? no('changes the network') : yes()
    case 'mount':
      return rest.length ? no('mounts') : yes()
    case 'dmesg':
      return has('-c', '-C', '-D', '-E', '-n', '--clear', '--read-clear', '--console-off', '--console-on', '--console-level') ? no('changes the kernel log') : yes()
    case 'journalctl':
      return rest.some(w => /^--(rotate|vacuum|flush|sync|relinquish|setup-keys|update-catalog)/.test(w)) ? no('changes the journal') : yes()
    case 'systemctl': {
      const sub = positional[0]
      return !sub || /^(status|show|is-active|is-enabled|is-failed|list-units|list-timers|list-unit-files|list-sockets|list-dependencies|cat|help)$/.test(sub)
        ? yes() : no(`${sub} changes a unit`)
    }
    case 'docker': case 'docker-compose':
      return docker(name === 'docker-compose' ? ['compose', ...rest] : rest) ? yes() : no('not a reading subcommand')
    case 'git':
      return git(rest)
    case 'gh':
      return gh(rest)
    case 'curl':
      return curl(rest, cfg)
    case 'openssl':
      return /^(x509|verify)$/.test(positional[0] ?? '') && !has('-out') ? yes() : no('not a reading subcommand')
    case 'kafka-topics': case 'kafka-topics.sh':
      return has('--describe', '--list') && !has('--create', '--delete', '--alter') ? yes() : no('changes topics')
    case 'kafka-consumer-groups': case 'kafka-consumer-groups.sh':
      return has('--describe', '--list') && !has('--reset-offsets', '--delete', '--delete-offsets', '--execute') ? yes() : no('changes groups')
    case 'kafka-configs': case 'kafka-configs.sh':
      return has('--describe') && !has('--alter') ? yes() : no('changes configs')
    case 'consul':
      return /^(members|info|version)$/.test(positional[0] ?? '')
        || (positional[0] === 'catalog' && /^(services|nodes|datacenters)$/.test(positional[1] ?? ''))
        || (positional[0] === 'operator' && positional[1] === 'raft' && positional[2] === 'list-peers')
        || (positional[0] === 'kv' && positional[1] === 'get') ? yes() : no('not a reading subcommand')
    case 'nomad':
      return /^(status|version)$/.test(positional[0] ?? '')
        || (/^(job|node)$/.test(positional[0] ?? '') && /^(status|inspect|history)$/.test(positional[1] ?? ''))
        || (positional[0] === 'alloc' && /^(status|logs)$/.test(positional[1] ?? ''))
        || (positional[0] === 'server' && positional[1] === 'members') ? yes() : no('not a reading subcommand')
    case 'sudo': {
      let k = 0
      while (k < rest.length && rest[k].startsWith('-')) {
        if (/^-[^nuHE]|^-[nuHE]*[^nuHE]/.test(rest[k]) && rest[k] !== '-u') return no(`${rest[k]} is not a plain run as another user`)
        k += rest[k] === '-u' || /^-[nHE]*u$/.test(rest[k]) ? 2 : 1
      }
      return command(rest.slice(k), cfg, depth)
    }
    case 'timeout': {
      let k = 0
      while (k < rest.length && rest[k].startsWith('-')) k += rest[k] === '-s' || rest[k] === '-k' ? 2 : 1
      return command(rest.slice(k + 1), cfg, depth)
    }
    case 'ssh':
      return ssh(rest, cfg, depth)
    default:
      return ask(`${name} is not on the read-only list`)
  }
}

/** A search over the whole home or the whole disk: it reads the credential stores with the rest. */
function everywhere(args: string[]): boolean {
  return args.some(w => /^(~\/?|\$HOME\/?|\/|\/Users(\/[^/]+)?\/?|\/home(\/[^/]+)?\/?|\/root\/?|\/etc\/?)$/.test(w))
}

function docker(args: string[]): boolean {
  const [sub, next] = args.filter(w => !w.startsWith('-'))
  if (sub === 'compose') return /^(ps|logs|config|images|top|version|ls)$/.test(next ?? '')
  if (sub === 'container') return /^(ls|list|inspect|logs|top|stats|port|diff)$/.test(next ?? '')
  if (sub === 'image') return /^(ls|list|inspect|history)$/.test(next ?? '')
  return /^(ps|logs|inspect|stats|images|top|version|info|port|history|diff)$/.test(sub ?? '')
}

function git(args: string[]): Verdict {
  let k = 0
  while (k < args.length && args[k].startsWith('-')) {
    if (args[k] === '-c' || args[k].startsWith('--config') || args[k].startsWith('--exec-path')) return ask('git: -c can run any command')
    k += args[k] === '-C' ? 2 : 1
  }
  const [sub, ...rest] = args.slice(k)
  const has = (...flags: string[]) => rest.some(w => flags.some(f => w === f || w.startsWith(`${f}=`)))
  if (rest.some(w => w.startsWith('--output') || w === '-O' || w.startsWith('--open-files-in-pager') || w.startsWith('--upload-pack') || w.startsWith('--ext-diff')))
    return ask(`git ${sub}: writes a file or runs a command`)
  const read = /^(log|show|diff|status|rev-parse|describe|blame|ls-files|ls-tree|cat-file|grep|shortlog|rev-list|merge-base|name-rev|whatchanged|count-objects|fetch|version|help)$/
  if (read.test(sub ?? '')) return allow(`git ${sub}`)
  const positional = rest.filter(w => !w.startsWith('-'))
  const ok = (sub === 'branch' && !positional.length && !has('-d', '-D', '-m', '-M', '-c', '-C', '--delete', '--move', '--copy', '--set-upstream-to', '-u', '--unset-upstream', '--edit-description', '-f', '--force'))
    || (sub === 'remote' && (!positional.length || /^(show|get-url)$/.test(positional[0])))
    || (sub === 'tag' && (!rest.length || has('-l', '--list', '--contains', '--points-at', '-n')) && !has('-d', '--delete', '-f', '--force', '-a', '-s', '-m'))
    || (sub === 'config' && has('--get', '--get-all', '--get-regexp', '--list', '-l'))
    || (/^(worktree|stash|notes)$/.test(sub ?? '') && /^(list|show)$/.test(positional[0] ?? ''))
    || (sub === 'reflog' && (!positional.length || positional[0] === 'show'))
  return ok ? allow(`git ${sub}`) : ask(`git ${sub ?? ''}: not a reading subcommand`)
}

function gh(args: string[]): Verdict {
  const words = args.filter((w, k) => !(w === '-R' || w === '--repo' || args[k - 1] === '-R' || args[k - 1] === '--repo'))
  const [group, sub] = words.filter(w => !w.startsWith('-'))
  if (group === 'api') {
    const rest = words.slice(words.indexOf('api') + 1)
    if (rest.some(w => /^(-f|-F|--field|--raw-field|--input)(=|$)/.test(w) || /^-[a-zA-Z]*[fF]$/.test(w))) return ask('gh api with fields posts')
    const method = rest.find((w, k) => rest[k - 1] === '-X' || rest[k - 1] === '--method') ?? rest.find(w => w.startsWith('--method='))?.split('=')[1]
    if (method && !/^(GET|HEAD)$/i.test(method)) return ask(`gh api ${method}`)
    return sub === 'graphql' ? ask('gh api graphql posts (a mutation is a query too)') : allow('gh api GET')
  }
  if (group === 'search') return allow('gh search')
  const reads: Record<string, RegExp> = {
    pr: /^(view|list|diff|checks|status)$/, issue: /^(view|list|status)$/, run: /^(view|list)$/,
    workflow: /^(view|list)$/, repo: /^(view|list)$/, release: /^(view|list)$/,
  }
  return reads[group ?? '']?.test(sub ?? '') ? allow(`gh ${group} ${sub}`) : ask(`gh ${group ?? ''} ${sub ?? ''}: not a reading subcommand`)
}

// sed scripts that only print: line or pattern ranges with p, d, q, =, n, N, P, D, l, and substitutions whose flags
// neither write (w) nor run (e) — anything else (r, R, w, W, e, y, a labelled branch…) asks
const SED_ADDR = String.raw`(?:\d+|\$|/(?:\\.|[^/\\\n])*/)`
const SED_CMD = String.raw`(?:${SED_ADDR}(?:,${SED_ADDR})?)?!?\s*(?:[pdqnNPDl=]|s(.)(?:\\.|(?!\1)[^\n])*\1(?:\\.|(?!\1)[^\n])*\1[gpiImM0-9]*)`
const SED_SAFE = new RegExp(String.raw`^\s*${SED_CMD}(?:\s*[;\n]\s*${SED_CMD.replace(/\\1/g, '\\2')})*\s*;?\s*$`)

const CURL_FLAGS = /^-[sSLfkiIvg46N]+$/
const CURL_LONG = /^--(silent|show-error|location|fail|fail-with-body|insecure|include|head|verbose|compressed|globoff|http1\.1|http2|ipv4|ipv6|no-progress-meter|no-buffer)$/
const CURL_WITH_ARG = /^(-H|-A|-u|-m|-w|-e|-b|-E|--header|--user|--user-agent|--max-time|--connect-timeout|--write-out|--retry|--retry-delay|--cacert|--cert|--key|--noproxy|--referer|--cookie)$/

function curl(args: string[], cfg: Config): Verdict {
  const urls: string[] = []
  for (let k = 0; k < args.length; k++) {
    const w = args[k]
    if (w === '-X' || w === '--request') {
      if (!/^(GET|HEAD)$/i.test(args[k + 1] ?? '')) return ask(`curl ${args[k + 1] ?? ''}`)
      k += 1
    } else if (w === '--url') {
      urls.push(args[++k] ?? '')
    } else if (CURL_WITH_ARG.test(w)) {
      k += 1
    } else if (/^--[a-z-]+=/.test(w) && CURL_WITH_ARG.test(w.split('=')[0])) {
      continue
    } else if (CURL_FLAGS.test(w) || CURL_LONG.test(w)) {
      continue
    } else if (w.startsWith('-')) {
      return ask(`curl ${w}: sends, writes a file or is not known here`)
    } else {
      urls.push(w)
    }
  }
  if (!urls.length) return ask('curl without an address')
  for (const url of urls) {
    const host = /^(?:https?:\/\/)?(?:[^@/]*@)?(\[[^\]]+\]|[^:/?#]+)/i.exec(url)?.[1]?.toLowerCase() ?? ''
    if (!/^https?:\/\//i.test(url) && url.includes('://')) return ask(`curl ${url}: not http(s)`)
    if (!/^(localhost|127\.0\.0\.1|\[::1\])$/.test(host) && !matches(host, cfg.curlHosts)) return ask(`curl to ${host || url}: not a host it may read`)
  }
  return allow('curl GET')
}

const SSH_FLAG_ARG = /^-[pilmcbeBQ]$/
const SSH_OPTION = /^(ConnectTimeout|BatchMode|LogLevel|ServerAliveInterval|ServerAliveCountMax)=|^StrictHostKeyChecking=(yes|accept-new)$/i

function ssh(args: string[], cfg: Config, depth: number): Verdict {
  let k = 0
  for (; k < args.length && args[k].startsWith('-'); k++) {
    const w = args[k]
    if (w === '-o') {
      if (!SSH_OPTION.test(args[k + 1] ?? '')) return ask(`ssh -o ${args[k + 1] ?? ''}`)
      k += 1
    } else if (w === '-J') {
      // through a bastion: every hop must be a host it may read
      const hops = (args[k + 1] ?? '').split(',').map(h => h.split('@').pop()?.split(':')[0] ?? '')
      if (!hops.every(h => h && matches(h, cfg.sshHosts))) return ask(`ssh -J ${args[k + 1] ?? ''}: not a host it may read`)
      k += 1
    } else if (SSH_FLAG_ARG.test(w)) {
      k += 1
    } else if (!/^-[46AaCfGgKkMNnqsTtVvXxYy]+$/.test(w)) {
      return ask(`ssh ${w}: a tunnel, a proxy or a file`)
    }
  }
  const host = (args[k] ?? '').split('@').pop() ?? ''
  if (!host) return ask('ssh without a host')
  if (!matches(host, cfg.sshHosts)) return ask(`ssh to ${host}: not a host it may read`)
  const remote = args.slice(k + 1).join(' ').trim()
  if (!remote) return ask(`ssh ${host}: an interactive shell`)
  const v = shell(remote, cfg, depth + 1)
  return v.decision === 'allow' ? allow(`ssh ${host}: ${v.reason}`) : ask(`on ${host}: ${v.reason}`)
}

function matches(host: string, patterns: string[]): boolean {
  return patterns.some(p => p && new RegExp(`^${p.split('*').map(s => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('[A-Za-z0-9._-]*')}$`, 'i').test(host))
}
