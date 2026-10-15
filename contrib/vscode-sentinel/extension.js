// vscode://sentinel.sentinel-claude/zabbix?event=<id> → a terminal running Claude Code on that problem.
// The link carries the event id only: the prompt comes from Sentinel, which answers for problems still open.
const os = require('os');
const vscode = require('vscode');

async function open(uri) {
  const event = new URLSearchParams(uri.query).get('event') || '';
  if (uri.path !== '/zabbix' || !/^\d+$/.test(event)) {
    vscode.window.showErrorMessage(`Sentinel: unknown link ${uri.path}`);
    return;
  }
  const cfg = vscode.workspace.getConfiguration('sentinel');
  let alert;
  try {
    const r = await fetch(`${cfg.get('url').replace(/\/$/, '')}/api/zabbix/${event}/claude`);
    if (!r.ok) throw new Error(r.status === 404 ? 'this problem is no longer open in Sentinel' : `Sentinel answered ${r.status}`);
    alert = await r.json();
  } catch (e) {
    vscode.window.showErrorMessage(`Sentinel: ${e.message}`);
    return;
  }
  const cwd = cfg.get('cwd').replace(/^~(?=\/|$)/, os.homedir()) || undefined;
  // the prompt travels in the environment: nothing of it is parsed by the shell
  const terminal = vscode.window.createTerminal({ name: alert.title, cwd, env: { SENTINEL_PROMPT: alert.prompt } });
  terminal.show();
  terminal.sendText(`${cfg.get('command')} "$SENTINEL_PROMPT"`);
}

exports.activate = (context) => context.subscriptions.push(vscode.window.registerUriHandler({ handleUri: open }));
exports.deactivate = () => {};
