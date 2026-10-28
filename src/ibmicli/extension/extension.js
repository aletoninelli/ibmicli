/* ibmicli for VS Code.
 *
 * The CLI owns IBM i mappings, copy resolution, symbol parsing, task lists,
 * debug configuration, member comparisons and SQL preparation. This module
 * presents those results without duplicating their rules.
 * Tasks use the $ibmicli problem matcher for file:line:column diagnostics.
 * IBM.ibmidebug performs debugging; credentials remain in launch memory.
 * Plain bundled JavaScript keeps installation independent of npm builds. */

'use strict';

const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

/* Match by language and extension so new VS Code installations also work with plain-text RPG files. */
const SELECTOR = [
  { scheme: 'file', language: 'rpgle' },
  { scheme: 'file', language: 'sqlrpgle' },
  { scheme: 'file', pattern: '**/*.{rpgle,sqlrpgle,rpgleinc,rpgleh,sqlrpgleinc}' },
  { scheme: 'file', pattern: '**/*.{RPGLE,SQLRPGLE,RPGLEINC,RPGLEH}' }
];

/* Detect whether the cursor is on a directive; the CLI still resolves its target. */
const DIRECTIVE = /^\s*\/(?:copy|include)\s+([A-Za-z0-9$#@_.*/]+(?:,[A-Za-z0-9$#@_.]+|\([A-Za-z0-9$#@_.]+\)))/i;

const CHANNEL = vscode.window.createOutputChannel('ibmicli');

/* Reuse one terminal so old compiler listings do not accumulate in separate tabs. */
const TERMINAL = 'ibmicli';

// Pass the project to the CLI with -proj; see projectArgs.
const PROJECT_FILE = 'ibmiproj.toml';

/* Remember the last task per workspace and put it first for F6 followed by Enter. */
const LAST_TASK = 'ibmicli.lastTask';

/* Remember SEP procedure, user and condition per file, without passwords. */
const LAST_SEP = 'ibmicli.lastSep';

/* The IBM debugger is installed with the companion extensions by vscode install. */
const IBM_DEBUG = 'IBM.ibmidebug';

/* Bound the wait between separate SEP requests so an unclosed session cannot hang the command. */
const SEP_STEP_TIMEOUT = 20000;

let executable = null;       // Resolve the ibmicli executable once.
let generation = 0;          // Increment on save to invalidate caches.
const copyCache = new Map(); // file -> { key, promise }
const symbolCache = new Map();

/* ------------------------------------------------------------------ CLI -- */

function candidates() {
  const configured = vscode.workspace.getConfiguration('ibmicli').get('executable');
  if (configured) return [configured];
  return [
    'ibmicli',
    path.join(os.homedir(), '.local', 'bin', 'ibmicli'),
    '/opt/homebrew/bin/ibmicli',
    '/usr/local/bin/ibmicli'
  ];
}

/* VS Code launched from the Dock may not inherit the shell PATH; try standard uv and package-manager locations. */
function resolveExecutable() {
  if (executable) return executable;
  for (const candidate of candidates()) {
    try {
      const probe = cp.spawnSync(candidate, ['--version'], { encoding: 'utf8', timeout: 10000 });
      if (probe.status === 0) {
        executable = candidate;
        CHANNEL.appendLine(`ibmicli: ${candidate} (${(probe.stdout || '').trim()})`);
        return executable;
      }
    } catch (error) {
      /* Try the next candidate. */
    }
  }
  return null;
}

function missingExecutable() {
  vscode.window
    .showErrorMessage(
      'Command "ibmicli" not found. Install the CLI or set its path in ibmicli.executable.',
      'Open settings'
    )
    .then((choice) => {
      if (choice) {
        vscode.commands.executeCommand('workbench.action.openSettings', 'ibmicli.executable');
      }
    });
}

/* Run the CLI and parse its JSON response. Expected failures can be JSON error fields.
 * input supplies an editor buffer; lines parses one record per statement;
 * timeout is milliseconds (0 means unlimited); token supports cancellation.
 * Log only stderr: debug stdout contains credentials. */
function cli(args, cwd, options) {
  const settings = options || {};
  const command = resolveExecutable();
  if (!command) {
    missingExecutable();
    return Promise.reject(new Error('ibmicli not found'));
  }
  return new Promise((resolve, reject) => {
    const child = cp.execFile(
      command,
      args,
      {
        cwd: cwd,
        maxBuffer: settings.lines ? 512 * 1024 * 1024 : 32 * 1024 * 1024,
        timeout: settings.timeout === undefined ? 120000 : settings.timeout
      },
      (error, stdout, stderr) => {
        const text = (stdout || '').trim();
        if (text) {
          try {
            resolve(
              settings.lines
                ? text.split('\n').filter((line) => line.trim()).map((line) => JSON.parse(line))
                : JSON.parse(text)
            );
            return;
          } catch (parseError) {
            /* Fall through to error reporting. */
          }
        }
        const stopped = settings.token && settings.token.isCancellationRequested;
        const detail = stopped
          ? 'stopped'
          : (stderr || '').trim() || (error && error.message) || 'no response';
        // Arguments may include selected SQL; log only the beginning.
        CHANNEL.appendLine(`ibmicli ${args.join(' ').slice(0, 300)}\n  ${detail}`);
        reject(new Error(detail));
      }
    );
    child.stdin.on('error', () => {}); // The process can exit before reading stdin.
    child.stdin.end(settings.input || '');
    if (settings.token) settings.token.onCancellationRequested(() => child.kill());
  });
}

function workspaceOf(document) {
  const folder = vscode.workspace.getWorkspaceFolder(document.uri);
  return folder ? folder.uri.fsPath : path.dirname(document.uri.fsPath);
}

/* Find the nearest project from the file up to its workspace root, never above it. Pass -proj explicitly; the CLI performs no discovery. */
function projectArgs(document) {
  const folder = vscode.workspace.getWorkspaceFolder(document.uri);
  if (!folder) return [];
  const top = folder.uri.fsPath;
  let here = path.dirname(document.uri.fsPath);
  while (here === top || here.startsWith(top + path.sep)) {
    const candidate = path.join(here, PROJECT_FILE);
    if (fs.existsSync(candidate)) return ['-proj', candidate];
    if (here === top) break;
    here = path.dirname(here);
  }
  return [];
}

/* --------------------------------------------------------------- Copies -- */

/* Cache by directive text and save generation so typing does not repeatedly invoke the CLI, while downloaded copies are refreshed. */
function copyKey(document) {
  const lines = [];
  for (let index = 0; index < document.lineCount; index += 1) {
    const text = document.lineAt(index).text;
    if (DIRECTIVE.test(text)) lines.push(index + ':' + text.trim());
  }
  return generation + '\n' + lines.join('\n');
}

function copies(document) {
  const file = document.uri.fsPath;
  const key = copyKey(document);
  const cached = copyCache.get(file);
  if (cached && cached.key === key) return cached.promise;

  const promise = cli(
    ['copy', '-from', file, '-all', '--json', ...projectArgs(document)],
    workspaceOf(document)
  ).then((data) => (data && data.copies) || []);
  copyCache.set(file, { key: key, promise: promise });
  promise.catch(() => copyCache.delete(file));
  return promise;
}

function copyAt(document, position) {
  const text = document.lineAt(position.line).text;
  return DIRECTIVE.test(text) ? text : null;
}

/* Ask before creating a local source file, showing its already-resolved destination without a network request. */
async function confirmPull(found) {
  if (!vscode.workspace.getConfiguration('ibmicli').get('confirmPull')) return true;
  const answer = await vscode.window.showInformationMessage(
    `${found.remote} is missing locally. Download it?`,
    { modal: true, detail: `This file will be created:\n${found.path}` },
    'Download'
  );
  return answer === 'Download';
}

/* Resolve locally first, then pull only after confirmation. A declined download returns null without an error. */
async function openCopy(document, line, options) {
  const settings = vscode.workspace.getConfiguration('ibmicli');
  const where = workspaceOf(document);
  const spec = line.trim();

  const data = await cli(
    ['copy', spec, '-from', document.uri.fsPath, '-no-pull', '--json', ...projectArgs(document)],
    where
  );
  const found = (data.copies || [])[0];
  if (!found) throw new Error(data.error || 'unresolved copy');
  if (found.error) throw new Error(found.error);
  if (found.exists) return vscode.Uri.file(found.path);

  if (!settings.get('pullOnOpen')) {
    throw new Error(`${found.remote}: missing locally (${found.path})`);
  }
  if (!(options && options.silent) && !(await confirmPull(found))) return null;

  const pulled = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window, title: `ibmicli: downloading ${found.remote}` },
    () => cli(
      ['copy', spec, '-from', document.uri.fsPath, '-pull', '--json', ...projectArgs(document)],
      where
    )
  );
  const done = (pulled.copies || [])[0];
  if (!done || done.error || !done.exists) {
    throw new Error((done && done.error) || `${found.remote}: download failed`);
  }
  generation += 1; // Reread symbols after downloading.
  return vscode.Uri.file(done.path);
}

/* -------------------------------------------------------------- Symbols -- */

/* Use copy symbols for completion and navigation to avoid duplicates from other providers. All symbols support local likeds-qualified references. */
function symbolData(document) {
  if (!vscode.workspace.getConfiguration('ibmicli').get('completion')) {
    return Promise.resolve({ all: [], copies: [] });
  }
  const file = document.uri.fsPath;
  const key = copyKey(document);
  const cached = symbolCache.get(file);
  if (cached && cached.key === key) return cached.promise;

  const promise = cli(
    ['symbols', file, '--json', ...projectArgs(document)], workspaceOf(document)
  ).then((data) => {
    const all = (data && data.symbols) || [];
    return { all: all, copies: all.filter((s) => s.file !== data.file) };
  });
  symbolCache.set(file, { key: key, promise: promise });
  promise.catch(() => symbolCache.delete(file));
  return promise;
}

function symbols(document) {
  return symbolData(document).then((data) => data.copies);
}

/* Qualifier names before the cursor, skipping nested array indexes. Return null when no dot precedes the cursor. */
function qualifier(before) {
  let index = before.length;
  while (index > 0 && /[\w$#@\u00a3]/.test(before[index - 1])) index -= 1;
  const names = [];
  while (index > 0 && before[index - 1] === '.') {
    index -= 1;
    if (before[index - 1] === ')') {
      let depth = 0;
      while (index > 0) {
        index -= 1;
        if (before[index] === ')') depth += 1;
        else if (before[index] === '(' && (depth -= 1) === 0) break;
      }
      if (depth !== 0) return null;
    }
    const end = index;
    while (index > 0 && /[\w$#@\u00a3]/.test(before[index - 1])) index -= 1;
    if (index === end) return null;
    names.unshift(before.slice(index, end));
  }
  return names.length ? names : null;
}

/* Resolve the structure template through chained likeds references, or use the full symbol name. */
function shape(all, symbol) {
  for (let depth = 0; symbol.like && depth < 10; depth += 1) {
    const like = symbol.like.toLowerCase();
    const target = all.find((s) => !s.parent && s.kind === 'struct' && s.name.toLowerCase() === like);
    if (!target) return like;
    symbol = target;
  }
  return (symbol.parent ? `${symbol.parent}.${symbol.name}` : symbol.name).toLowerCase();
}

/* Descend qualified names one field at a time to find the fields of a.b. */
function members(all, names) {
  const first = names[0].toLowerCase();
  let node =
    all.find((s) => !s.parent && s.name.toLowerCase() === first) ||
    all.find((s) => s.kind === 'subfield' && s.name.toLowerCase() === first);
  for (const name of names.slice(1)) {
    if (!node) return [];
    const parent = shape(all, node);
    const lower = name.toLowerCase();
    node = all.find(
      (s) => s.kind === 'subfield' && s.parent.toLowerCase() === parent && s.name.toLowerCase() === lower
    );
  }
  if (!node) return [];
  const parent = shape(all, node);
  return all.filter((s) => s.kind === 'subfield' && s.parent.toLowerCase() === parent);
}

const COMPLETION_KIND = {
  procedure: vscode.CompletionItemKind.Function,
  prototype: vscode.CompletionItemKind.Function,
  variable: vscode.CompletionItemKind.Variable,
  constant: vscode.CompletionItemKind.Constant,
  struct: vscode.CompletionItemKind.Struct,
  subfield: vscode.CompletionItemKind.Field,
  file: vscode.CompletionItemKind.File,
  subroutine: vscode.CompletionItemKind.Method
};

function documentation(symbol) {
  const markdown = new vscode.MarkdownString();
  if (symbol.doc) markdown.appendMarkdown(symbol.doc.replace(/\n/g, '  \n') + '\n\n');
  if (symbol.signature) markdown.appendCodeblock(symbol.signature, 'rpgle');
  if (symbol.file) {
    const name = path.basename(path.dirname(symbol.file)) + '/' + path.basename(symbol.file);
    markdown.appendMarkdown(`\n_${name}:${symbol.line}_`);
  }
  return markdown;
}

/* Insert procedures as snippets with parameter tab stops. */
function callSnippet(symbol) {
  if (!symbol.params || !symbol.params.length) {
    return new vscode.SnippetString(`${symbol.name}()`);
  }
  const parts = symbol.params.map(
    (parameter, index) => '${' + (index + 1) + ':' + parameter.name + '}'
  );
  return new vscode.SnippetString(`${symbol.name}(${parts.join(' : ')})`);
}

/* ---------------------------------------------------------------- Tasks -- */

/* The CLI selects global and project tasks and filters their extensions; no compilation rules belong here. */
function tasksFor(document) {
  const name = path.basename(document.uri.fsPath);
  const dot = name.lastIndexOf('.');
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  const args = ['tasks', '--json', ...projectArgs(document)];
  if (extension) args.push('-e', extension);
  return cli(args, workspaceOf(document)).then((data) => (data && data.tasks) || []);
}

/* Quote terminal arguments, including spaces and embedded apostrophes. */
function quoted(text) {
  return "'" + String(text).replace(/'/g, "'\\''") + "'";
}

/* Display task names prominently because they are command identifiers. Labels describe them; only definition problems need another line. */
function taskItems(found, last, document) {
  const items = found.map((task) => ({
    label: task.name,
    description: task.label && task.label !== task.name ? task.label : '',
    detail: (task.problems && task.problems.length)
      ? 'WARNING: ' + task.problems.join(' - ')
      : undefined,
    key: task.name,
    task: task
  }));
  /* Debug commands appear at the end; prefixed state keys keep them distinct from task names. */
  const debug = DEBUG_ITEMS.map((item) => Object.assign({ key: 'debug:' + item.command }, item));
  /* Read-only file actions: member comparison and SQL where its extension permits it. */
  const files = FILE_ITEMS
    .filter((item) => !item.sql || hasSql(document))
    .map((item) => Object.assign({ key: 'file:' + item.command }, item));
  /* Project-wide actions never move to the last-used position: habitual F6 and Enter must not select a bulk build. */
  const project = PROJECT_ITEMS.map((item) => Object.assign({ key: 'project:' + item.command }, item));
  const all = items.concat(debug, files);
  const first = all.filter((item) => item.key === last);
  const rest = items.filter((item) => item.key !== last);
  const separator = { label: 'debug', kind: vscode.QuickPickItemKind.Separator };
  const fileSeparator = { label: 'file', kind: vscode.QuickPickItemKind.Separator };
  const projectSeparator = { label: 'project', kind: vscode.QuickPickItemKind.Separator };
  return first.concat(
    rest,
    [separator],
    debug.filter((item) => item.key !== last),
    [fileSeparator],
    files.filter((item) => item.key !== last),
    [projectSeparator],
    project
  );
}

/* F6 actions that read the open file. */
const FILE_ITEMS = [
  {
    label: 'compare with IBM i',
    description: 'local file beside its IBM i member in the diff editor',
    command: 'ibmicli.diffRemote'
  },
  {
    label: 'run SQL',
    description: 'cursor statement or selected SQL, read-only',
    command: 'ibmicli.runSql',
    sql: true
  }
];

/* Source extensions that can contain executable SQL. */
function hasSql(document) {
  return /sql/i.test(path.extname(document.uri.fsPath));
}

/* F6 debug actions. */
const DEBUG_ITEMS = [
  {
    label: 'debug batch',
    description: 'submit and debug the program in batch (IBM i Debug)',
    command: 'ibmicli.debugBatch'
  },
  {
    label: 'service entry point',
    description: 'stop when the program is called (IBM i Debug)',
    command: 'ibmicli.debugSep'
  }
];

/* F6 actions for the project rather than the open file. */
const PROJECT_ITEMS = [
  {
    label: 'clear problems',
    description: 'remove the last build diagnostics from Problems and the editor',
    command: 'ibmicli.clearProblems'
  },
  {
    label: 'deploy-build-changed',
    description: 'upload and build Git-changed sources, with confirmation',
    command: 'ibmicli.deployBuildChanged'
  }
];

/* Run tasks in a visible terminal so long compiler output remains readable while the task runs. */
function runInTerminal(name, document) {
  runAsTask(
    `${name} ${path.basename(document.uri.fsPath)}`,
    `run ${name} -src ${quoted(document.uri.fsPath)}${projectLine(document)}`,
    document
  );
}

/* Leading-space -proj arguments for a terminal command. */
function projectLine(document) {
  const args = projectArgs(document);
  return args.length ? ` -proj ${quoted(args[1])}` : '';
}

/* VS Code tasks attach the $ibmicli problem matcher. Each run replaces prior
 * diagnostics, so successful compilation clears Problems. CLI paths and the
 * matcher both use the workspace root. Outside a workspace, fall back to
 * terminal output with clickable source locations. */
function runAsTask(title, args, document) {
  const command = resolveExecutable();
  if (!command) {
    missingExecutable();
    return;
  }
  const folder = vscode.workspace.getWorkspaceFolder(document.uri);
  const cwd = workspaceOf(document);
  if (!folder) {
    sendToTerminal(args, cwd, true);
    return;
  }
  const task = new vscode.Task(
    { type: 'ibmicli', args: args },
    folder,
    title,
    'ibmicli',
    new vscode.ShellExecution(`${quoted(command)} ${args}`, { cwd: cwd }),
    ['$ibmicli']
  );
  /* Share one output panel and keep focus in the editor. */
  task.presentationOptions = {
    reveal: vscode.TaskRevealKind.Always,
    panel: vscode.TaskPanelKind.Shared,
    focus: false,
    echo: true,
    clear: false,
    showReuseMessage: false
  };
  vscode.tasks.executeTask(task).then(undefined, (error) => {
    vscode.window.showErrorMessage(`ibmicli: could not start the task: ${error.message}`);
  });
}

/* Extensions cannot delete task diagnostics: VS Code owns them. A task with
 * the same matcher that reports nothing makes VS Code remove those from the
 * previous run. It starts no process and does not contact IBM i. */
async function clearProblems() {
  const editor = vscode.window.activeTextEditor;
  const folder = (editor && vscode.workspace.getWorkspaceFolder(editor.document.uri))
    || (vscode.workspace.workspaceFolders || [])[0];
  if (!folder) {
    vscode.window.showInformationMessage('ibmicli: no workspace folder, so there are no task problems to clear.');
    return;
  }
  const execution = new vscode.CustomExecution(async () => {
    const closed = new vscode.EventEmitter();
    return {
      onDidWrite: new vscode.EventEmitter().event,
      onDidClose: closed.event,
      open: () => closed.fire(0),
      close: () => {}
    };
  });
  const task = new vscode.Task(
    { type: 'ibmicli', args: '' },
    folder,
    'clear problems',
    'ibmicli',
    execution,
    ['$ibmicli']
  );
  /* Dedicated and hidden: the last compiler output stays in its terminal. */
  task.presentationOptions = {
    reveal: vscode.TaskRevealKind.Never,
    panel: vscode.TaskPanelKind.Dedicated,
    focus: false,
    echo: false,
    showReuseMessage: false,
    close: true
  };
  try {
    await vscode.tasks.executeTask(task);
  } catch (error) {
    vscode.window.showErrorMessage(`ibmicli: could not clear problems: ${error.message}`);
  }
}

/* Reuse the dedicated terminal, changing its working folder before commands whose scope depends on it. */
function sendToTerminal(args, cwd, moveTo) {
  const command = resolveExecutable();
  if (!command) {
    missingExecutable();
    return;
  }
  let terminal = vscode.window.terminals.find((item) => item.name === TERMINAL);
  if (!terminal) terminal = vscode.window.createTerminal({ name: TERMINAL, cwd: cwd });
  terminal.show(true);
  const prefix = moveTo ? `cd ${quoted(cwd)} && ` : '';
  terminal.sendText(`${prefix}${quoted(command)} ${args}`);
}

/* ------------------------------------------------- deploy-build-changed -- */

/* Limit visible confirmation plan rows and count the remaining entries. */
const PLAN_LINES = 25;

/* Bulk deploy/build requires modal confirmation of the CLI JSON plan before execution. */
async function deployBuildChanged(document) {
  if (vscode.workspace.getConfiguration('ibmicli').get('saveBeforeRun')) {
    /* Save buffers first because the CLI reads source files from disk. */
    await vscode.workspace.saveAll(false);
  }
  const cwd = workspaceOf(document);
  const plan = await cli(['deploy-build-changed', '--json', ...projectArgs(document)], cwd);
  const items = plan.items || [];
  const skipped = plan.skipped || [];
  if (!items.length) {
    vscode.window.showInformationMessage(
      skipped.length
        ? `ibmicli: no sources to upload (${skipped.length} skipped: 'ibmicli deploy-build-changed -n')`
        : 'ibmicli: no sources changed since the last commit'
    );
    return;
  }

  const count = (kind) => items.filter((item) => item.kind === kind).length;
  const summary = [
    `${items.length} files to upload`,
    `${count('*SRVPGM')} *SRVPGM and ${count('*PGM')} *PGM to build`
  ];
  if (skipped.length) summary.push(`${skipped.length} skipped due to problems`);
  const lines = items
    .slice(0, PLAN_LINES)
    .map((item) => `${item.kind === 'invio' ? 'upload' : item.kind}  ${item.file}${item.task ? '  ' + item.task : ''}`);
  if (items.length > PLAN_LINES) lines.push(`... and another ${items.length - PLAN_LINES}`);

  const go = 'Upload and build';
  const answer = await vscode.window.showWarningMessage(
    `Upload and build ${items.length} changed sources?`,
    {
      modal: true,
      detail: `${summary.join(', ')}.\n\n${lines.join('\n')}\n\nFolder: ${cwd}`
    },
    go
  );
  if (answer !== go) return;
  runAsTask('deploy-build-changed', `deploy-build-changed${projectLine(document)}`, document);
}

/* ---------------------------------------------------------------- Debug -- */

/* Require the IBM i Debug extension or explain how to install it. */
function hasDebugger() {
  if (vscode.extensions.getExtension(IBM_DEBUG)) return true;
  vscode.window.showErrorMessage(
    `ibmicli: debugging requires the IBM i Debug extension (${IBM_DEBUG}). ` +
      "Install it with 'ibmicli vscode install' and reload the window."
  );
  return false;
}

/* Obtain program launch settings from the CLI; keep the password only in the returned object. */
function debugData(document, sep) {
  const args = ['debug', document.uri.fsPath, '--json', ...projectArgs(document)];
  if (sep) args.push('-sep');
  return cli(args, workspaceOf(document));
}

/* Always use TLS with certificate verification and the debug hostname.
 * IBM opens a fresh verified connection for SEP hits, so an IP would fail.
 * trace stays false because tracing logs the launch configuration and password. */
function connection(data) {
  return {
    type: 'IBMiDebug',
    request: 'launch',
    host: data.host,
    port: data.port,
    secure: true,
    ignoreCertificateErrors: false,
    user: data.user,
    password: data.password,
    trace: false
  };
}

let launches = 0; // Unique debug session names.

/* Wait for each SEP request session to close before starting the next. Unique names identify sessions even if they close before startDebugging resolves. */
function launch(config, wait) {
  launches += 1;
  config.name = `${config.name} #${launches}`;
  if (!wait) return Promise.resolve(vscode.debug.startDebugging(undefined, config));

  return new Promise((resolve, reject) => {
    let session = null;
    let finished = false;
    const listeners = [];
    const finish = (value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      listeners.forEach((listener) => listener.dispose());
      resolve(value);
    };
    const timer = setTimeout(() => finish(true), SEP_STEP_TIMEOUT);
    listeners.push(
      vscode.debug.onDidStartDebugSession((started) => {
        if (!session && started.name === config.name) session = started;
      }),
      vscode.debug.onDidTerminateDebugSession((ended) => {
        if (session && ended.id === session.id) finish(true);
      })
    );
    vscode.debug.startDebugging(undefined, config).then(
      (started) => {
        if (!started) finish(false);
      },
      (error) => {
        finish(false);
        reject(error);
      }
    );
  });
}

/* IBM debug submits the CLI-provided batch command and attaches when the program starts. */
async function debugBatch(document) {
  const data = await debugData(document, false);
  const config = Object.assign(connection(data), {
    name: `ibmicli: debug ${data.library}/${data.program}`,
    subType: 'batch',
    library: data.library,
    program: data.program,
    startBatchJobCommand: data.startBatchJobCommand,
    updateProductionFiles: false
  });
  const started = await launch(config, false);
  if (!started) throw new Error('debug did not start: check the Debug Console');
}

/* Offer local procedures for SEP selection; allow manual input if parsing fails. */
async function proceduresOf(document) {
  try {
    const data = await cli(
      ['symbols', document.uri.fsPath, '--json', '-k', 'procedure', ...projectArgs(document)],
      workspaceOf(document)
    );
    return ((data && data.symbols) || [])
      .filter((symbol) => symbol.file === data.file)
      .map((symbol) => symbol.name);
  } catch (error) {
    return [];
  }
}

const WHOLE_PROGRAM = '(whole program)';

/* Select or type a procedure. Empty means the whole program; undefined means cancelled. */
function pickProcedure(procedures, last) {
  return new Promise((resolve) => {
    const pick = vscode.window.createQuickPick();
    const names = [WHOLE_PROGRAM].concat(procedures);
    const ordered = last && names.some((name) => name.toUpperCase() === last.toUpperCase())
      ? [names.find((name) => name.toUpperCase() === last.toUpperCase())]
          .concat(names.filter((name) => name.toUpperCase() !== last.toUpperCase()))
      : names;
    pick.items = ordered.map((name) => ({ label: name }));
    pick.placeholder =
      'Service-entry-point procedure (type a name; Enter selects the first)';
    pick.ignoreFocusOut = true;
    let answered = false;
    pick.onDidAccept(() => {
      const chosen = pick.selectedItems[0] ? pick.selectedItems[0].label : pick.value.trim();
      answered = true;
      pick.hide();
      resolve(chosen === WHOLE_PROGRAM ? '' : chosen);
    });
    pick.onDidHide(() => {
      pick.dispose();
      if (!answered) resolve(undefined);
    });
    pick.show();
  });
}

/* Create SEP requests in order: set, then set_user and set_condition, matching the IBM extension workflow. */
async function debugSep(document, context) {
  const data = await debugData(document, true);
  const remembered = context.workspaceState.get(LAST_SEP) || {};
  const last = remembered[data.file] || {};

  const procedure = await pickProcedure(await proceduresOf(document), last.procedure);
  if (procedure === undefined) return;

  const hitter = await vscode.window.showInputBox({
    prompt: `User triggering the SEP (empty = ${data.user})`,
    placeHolder: data.user,
    value: last.hitter || '',
    ignoreFocusOut: true
  });
  if (hitter === undefined) return;

  const condition = await vscode.window.showInputBox({
    prompt: 'Condition (optional), e.g. IP = \'192.0.2.33\'',
    value: last.condition || '',
    ignoreFocusOut: true
  });
  if (condition === undefined) return;

  /* IBM i procedure names are uppercase even when source spelling is mixed case. */
  const answers = {
    procedure: procedure.trim().toUpperCase(),
    hitter: hitter.trim().toUpperCase(),
    condition: condition.trim()
  };
  remembered[data.file] = answers;
  await context.workspaceState.update(LAST_SEP, remembered);

  /* A shared user without a condition could stop every web-server request. */
  if (answers.hitter && answers.hitter !== data.user && !answers.condition) {
    const answer = await vscode.window.showWarningMessage(
      `SEP without a condition for user ${answers.hitter}`,
      {
        modal: true,
        detail:
          `EVERY call to ${data.library}/${data.program} by ${answers.hitter} will stop, ` +
          'regardless of who made it. For a shared user, add a condition such as an IP filter.'
      },
      'Set anyway'
    );
    if (answer !== 'Set anyway') return;
  }

  /* Without a procedure, target all modules and procedures, as the IBM SEP view does. */
  const target = {
    library: data.library,
    program: data.program,
    programType: data.programType,
    module: answers.procedure ? data.module : '*ALL',
    procedure: answers.procedure || '*ALL',
    sepDaemonPort: data.sepDaemonPort
  };
  const step = (action, extra) =>
    Object.assign(connection(data), target, {
      name: `ibmicli: SEP ${data.program} ${action}`,
      subType: 'sep',
      action: action
    }, extra || {});

  if (!(await launch(step('set'), true))) {
    throw new Error('service entry point was not set: check the Debug Console');
  }
  if (answers.hitter && answers.hitter !== data.user) {
    await launch(step('set_user', { hitterUserProfile: answers.hitter }), true);
  }
  if (answers.condition) {
    await launch(step('set_condition', { condition: answers.condition }), true);
  }

  const where = `${data.library}/${data.program}` + (answers.procedure ? ` ${answers.procedure}` : '');
  const who = answers.hitter || data.user;
  vscode.window.showInformationMessage(
    `ibmicli: SEP on ${where}, user ${who}` +
      (answers.condition ? `, condition ${answers.condition}` : '') +
      '. Find it in the "Service Entry Points" view and remove it when finished.'
  );
}

/* ------------------------------------------------------------- Commands -- */

/* -------------------------------------------------------------- Outline -- */

/* Enable our Outline automatically only when the Halcyon provider is absent, avoiding duplicate entries. */
const RPGLE_EXTENSION = 'halcyontechltd.vscode-rpgle';

function outlineEnabled() {
  const mode = vscode.workspace.getConfiguration('ibmicli').get('outline');
  if (mode === 'on') return true;
  if (mode === 'off') return false;
  return !vscode.extensions.getExtension(RPGLE_EXTENSION);
}

const SYMBOL_KIND = {
  procedure: vscode.SymbolKind.Function,
  prototype: vscode.SymbolKind.Interface,
  variable: vscode.SymbolKind.Variable,
  constant: vscode.SymbolKind.Constant,
  struct: vscode.SymbolKind.Struct,
  subfield: vscode.SymbolKind.Field,
  file: vscode.SymbolKind.File,
  subroutine: vscode.SymbolKind.Method
};

const outlineCache = new Map(); // file -> { version, promise }

/* Read declarations from the editor buffer once per document version, including unsaved changes. */
function outlineSymbols(document) {
  const file = document.uri.fsPath;
  const cached = outlineCache.get(file);
  if (cached && cached.version === document.version) return cached.promise;
  const promise = cli(['symbols', file, '-local', '--stdin', '--json'], workspaceOf(document), {
    input: document.getText()
  }).then((data) => (data && data.symbols) || []);
  outlineCache.set(file, { version: document.version, promise: promise });
  promise.catch(() => outlineCache.delete(file));
  return promise;
}

/* Build the Outline tree using CLI scope and parent fields: procedures contain locals and subroutines, structures contain subfields. */
function outlineTree(document, symbols) {
  const last = document.lineCount - 1;
  const lines = (from, to) => new vscode.Range(from, 0, to, document.lineAt(to).text.length);
  const top = [];
  const structs = new Map(); // Path ('ds' or 'ds.child') -> node.
  const procedures = new Map();
  for (const symbol of symbols) {
    const start = Math.min(last, Math.max(0, symbol.line - 1));
    const end = Math.min(last, Math.max(start, (symbol.end_line || symbol.line) - 1));
    const node = new vscode.DocumentSymbol(
      symbol.name,
      symbol.detail || '',
      SYMBOL_KIND[symbol.kind] || vscode.SymbolKind.Variable,
      lines(start, end),
      lines(start, start)
    );
    if (symbol.kind === 'subfield') {
      /* Local DS names can repeat across procedures; children follow the most recently registered parent. */
      const parent = structs.get(symbol.parent.toLowerCase());
      (parent ? parent.children : top).push(node);
      structs.set(`${symbol.parent}.${symbol.name}`.toLowerCase(), node);
      continue;
    }
    const owner = symbol.scope ? procedures.get(symbol.scope.toLowerCase()) : null;
    (owner ? owner.children : top).push(node);
    if (symbol.kind === 'struct') structs.set(symbol.name.toLowerCase(), node);
    if (symbol.kind === 'procedure') procedures.set(symbol.name.toLowerCase(), node);
  }
  /* Extend an unknown parent end to contain all its children. */
  const widen = (node) => {
    for (const child of node.children) {
      widen(child);
      if (child.range.end.isAfter(node.range.end)) {
        node.range = new vscode.Range(node.range.start, child.range.end);
      }
    }
  };
  top.forEach(widen);
  return top;
}

/* ----------------------------------------------------------- Comparison -- */

/* Keep remote member documents read-only and in memory for the diff editor; discard them on close. */
const REMOTE_SCHEME = 'ibmicli-ibmi';
const remoteTexts = new Map(); // URI -> member text.
const remoteChanged = new vscode.EventEmitter();
let comparisons = 0;

async function diffRemote(document) {
  const file = document.uri.fsPath;
  const name = path.basename(file);
  const data = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window, title: `ibmicli: reading the member for ${name}` },
    () => cli(['diff', file, '--json', ...projectArgs(document)], workspaceOf(document))
  );
  if (data.error) throw new Error(`${data.remote}: ${data.error}`);
  /* The CLI compares the saved file; unsaved buffers must still open a VS Code diff. */
  if (data.same && !document.isDirty) {
    vscode.window.showInformationMessage(`ibmicli: ${name} matches ${data.remote}.`);
    return;
  }
  comparisons += 1;
  /* Keep the filename extension for language coloring. */
  const uri = vscode.Uri.from({
    scheme: REMOTE_SCHEME,
    path: `/${data.remote}/${name}`,
    query: String(comparisons)
  });
  remoteTexts.set(uri.toString(), data.text);
  remoteChanged.fire(uri);
  await vscode.commands.executeCommand(
    'vscode.diff',
    uri,
    document.uri,
    `${name}: ${data.remote} (IBM i) \u2194 local`
  );
}

/* ------------------------------------------------------------------ SQL -- */

/* Run cursor or selected SQL through db in read-only mode. The CLI prepares exec sql, removes INTO and identifies required host variables. */
const LAST_SQL_VARS = 'ibmicli.lastSqlVars';
let sqlPanel = null;

async function runSql(editor, context) {
  const document = editor.document;
  const file = document.uri.fsPath;
  const cwd = workspaceOf(document);
  const selection = editor.selection;
  /* Use the current editor buffer for cursor SQL, or the selection after -- so leading hyphens cannot become options. */
  const where = selection.isEmpty
    ? ['-line', String(selection.active.line + 1), '--stdin']
    : ['--', document.getText(selection)];
  const input = selection.isEmpty ? document.getText() : undefined;

  /* Put -proj before where, which can end with -- and SQL text. */
  const proj = projectArgs(document);
  const plan = await cli(['db', '-from', file, '--json', '-n', ...proj].concat(where), cwd, {
    input: input
  });
  const statements = plan.statements || [];
  if (!statements.length) {
    vscode.window.showInformationMessage('ibmicli: no SQL statement to run.');
    return;
  }

  /* Request host-variable SQL values, proposing the previous values. */
  const names = [];
  statements.forEach((statement) => (statement.vars || []).forEach((name) => {
    if (!names.includes(name)) names.push(name);
  }));
  const remembered = context.workspaceState.get(LAST_SQL_VARS) || {};
  const assignments = [];
  for (const name of names) {
    const value = await vscode.window.showInputBox({
      prompt: `Value for :${name}, written as SQL (e.g. 'A01' or 123)`,
      value: remembered[name] || '',
      ignoreFocusOut: true,
      validateInput: (text) => (text.trim() ? null : 'a value is required')
    });
    if (value === undefined) return;
    remembered[name] = value.trim();
    assignments.push('--var', `${name}=${value.trim()}`);
  }
  if (names.length) await context.workspaceState.update(LAST_SQL_VARS, remembered);

  const line = plan.start ? `:${plan.start}` : '';
  const results = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `ibmicli: SQL from ${path.basename(file)}${line}`,
      cancellable: true
    },
    (progress, token) =>
      cli(['db', '-from', file, '--json', ...proj].concat(assignments, where), cwd, {
        input: input,
        lines: true,
        timeout: 0,
        token: token
      })
  );
  showSqlResults(results, `SQL ${path.basename(file)}${line}`, plan.libraries || []);
}

function showSqlResults(results, title, libraries) {
  if (!sqlPanel) {
    sqlPanel = vscode.window.createWebviewPanel(
      'ibmicliSql',
      title,
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: false, retainContextWhenHidden: true }
    );
    sqlPanel.onDidDispose(() => {
      sqlPanel = null;
    });
  } else {
    sqlPanel.reveal(undefined, true);
  }
  sqlPanel.title = title;
  sqlPanel.webview.html = sqlHtml(results, libraries);
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Show SQL, results, row counts or errors for each statement. The result webview runs no scripts. */
function sqlHtml(results, libraries) {
  const sections = results.map((result) => {
    const sql = `<pre class="sql">${escapeHtml(result.sql || '')}</pre>`;
    if (result.error) {
      const notes = (result.notes || []).map((note) => `<p class="note">${escapeHtml(note)}</p>`).join('');
      return `<section>${sql}<p class="error">${escapeHtml(result.error)}</p>${notes}</section>`;
    }
    const columns = result.columns || [];
    const rows = result.rows || [];
    if (!columns.length) {
      const count = result.rowcount > 0 ? `, ${result.rowcount} rows` : '';
      return `<section>${sql}<p class="meta">executed${count}</p></section>`;
    }
    const head = columns.map((name) => `<th>${escapeHtml(name)}</th>`).join('');
    const body = rows
      .map((row) => {
        const cells = columns.map((name) => {
          const value = row[name];
          if (value === null || value === undefined) return '<td class="null">NULL</td>';
          const number = typeof value === 'number' ? ' class="number"' : '';
          return `<td${number}>${escapeHtml(value)}</td>`;
        });
        return `<tr>${cells.join('')}</tr>`;
      })
      .join('');
    const count = rows.length === 1 ? '1 row' : `${rows.length} rows`;
    return (
      `<section>${sql}<p class="meta">${count}</p>` +
      `<div class="grid"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></section>`
    );
  });
  const where = libraries.length
    ? `read-only, libraries ${escapeHtml(libraries.join(' '))}`
    : 'read-only';
  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size);
         color: var(--vscode-foreground); background: var(--vscode-editor-background); padding: 0 12px 24px; }
  header { color: var(--vscode-descriptionForeground); margin: 10px 0; }
  section { margin: 0 0 22px; }
  pre.sql { font-family: var(--vscode-editor-font-family); font-size: var(--vscode-editor-font-size);
            background: var(--vscode-textCodeBlock-background); padding: 8px 10px; margin: 0;
            white-space: pre-wrap; border-radius: 3px; }
  .meta, .note { color: var(--vscode-descriptionForeground); margin: 6px 0; }
  .error { color: var(--vscode-errorForeground); margin: 6px 0; font-weight: 600; }
  .grid { overflow: auto; max-height: 75vh; border: 1px solid var(--vscode-panel-border); }
  table { border-collapse: collapse; font-family: var(--vscode-editor-font-family);
          font-size: var(--vscode-editor-font-size); }
  th { position: sticky; top: 0; background: var(--vscode-editor-background); text-align: left;
       border-bottom: 1px solid var(--vscode-panel-border); }
  th, td { padding: 2px 10px; white-space: pre; }
  tbody tr:hover { background: var(--vscode-list-hoverBackground); }
  td.number { text-align: right; }
  td.null { color: var(--vscode-descriptionForeground); font-style: italic; }
</style></head>
<body><header>ibmicli db, ${where}</header>${sections.join('')}</body></html>`;
}

/* Unresolved copies are not compiler defects; report them when opened instead of marking every dependent source as problematic. Tasks own compiler diagnostics. */

/* Command URI used as a link target; VS Code runs it when clicked. */
function command(name, argument) {
  return vscode.Uri.parse(
    `command:${name}?${encodeURIComponent(JSON.stringify([argument]))}`
  );
}

/* ----------------------------------------------------------- Activation -- */

function activate(context) {
  const subscribe = (item) => context.subscriptions.push(item);
  subscribe(CHANNEL);

  /* Resolve missing copy targets only on click, so merely opening a source never downloads its dependencies. */
  subscribe(
    vscode.languages.registerDocumentLinkProvider(SELECTOR, {
      async provideDocumentLinks(document) {
        let found;
        try {
          found = await copies(document);
        } catch (error) {
          return [];
        }
        const links = [];
        for (const copy of found) {
          const line = Math.max(0, copy.line - 1);
          const range = new vscode.Range(line, copy.start, line, copy.end);
          const link = new vscode.DocumentLink(range);
          if (copy.exists) {
            /* Existing files open directly without another CLI invocation. */
            link.target = vscode.Uri.file(copy.path);
            link.tooltip = copy.path;
          } else {
            /* Route missing targets through the command that confirms downloads and explains errors. Recompute in case the destination has since been created. */
            link.target = command('ibmicli.openCopy', {
              file: document.uri.fsPath,
              line: copy.line
            });
            link.tooltip = copy.error || `${copy.remote}: missing locally`;
          }
          links.push(link);
        }
        return links;
      }
    })
  );

  /* F12 and Cmd+click navigate to copy members or symbol declarations inside copies. */
  subscribe(
    vscode.languages.registerDefinitionProvider(SELECTOR, {
      async provideDefinition(document, position) {
        const line = copyAt(document, position);
        if (line) {
          try {
            const uri = await openCopy(document, line);
            return uri ? new vscode.Location(uri, new vscode.Position(0, 0)) : null;
          } catch (error) {
            vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
            return null;
          }
        }

        const range = document.getWordRangeAtPosition(position, /[A-Za-z$#@_\u00a3][\w$#@\u00a3]*/);
        if (!range) return null;
        const word = document.getText(range).toLowerCase();
        const found = (await symbols(document)).filter((s) => s.name.toLowerCase() === word);
        return found.map(
          (s) =>
            new vscode.Location(
              vscode.Uri.file(s.file),
              new vscode.Position(Math.max(0, s.line - 1), 0)
            )
        );
      }
    })
  );

  subscribe(
    vscode.languages.registerCompletionItemProvider(SELECTOR, {
      async provideCompletionItems(document, position) {
        const line = document.lineAt(position.line).text;
        if (DIRECTIVE.test(line)) return []; // No symbol completion on directives.
        let data;
        try {
          data = await symbolData(document);
        } catch (error) {
          return [];
        }
        /* Set the replacement range explicitly: VS Code may exclude $ from a word and otherwise duplicate it. */
        const word = document.getWordRangeAtPosition(position, /[A-Za-z$#@_\u00a3][\w$#@\u00a3]*/);
        const range = new vscode.Range(word ? word.start : position, position);

        /* After a dot, offer only fields of the resolved structure; an unrelated symbol list would be noise. */
        const names = qualifier(line.slice(0, position.character));
        if (names) {
          return members(data.all, names).map((symbol) => {
            const item = new vscode.CompletionItem(symbol.name, vscode.CompletionItemKind.Field);
            item.detail = symbol.detail;
            item.documentation = documentation(symbol);
            item.range = range;
            item.sortText = '!' + symbol.name;
            return item;
          });
        }

        return data.copies.map((symbol) => {
          const item = new vscode.CompletionItem(
            symbol.name,
            COMPLETION_KIND[symbol.kind] || vscode.CompletionItemKind.Text
          );
          item.detail = symbol.parent
            ? `${symbol.parent}.${symbol.name} ${symbol.detail}`
            : symbol.detail;
          item.documentation = documentation(symbol);
          if (symbol.kind === 'prototype' || symbol.kind === 'procedure') {
            item.insertText = callSnippet(symbol);
          }
          item.range = range;
          /* Place copy suggestions after local VS Code suggestions so external prototypes cannot hide recently declared names. */
          item.sortText = 'zz' + symbol.name;
          return item;
        });
      }
    }, '.')
  );

  subscribe(
    vscode.languages.registerHoverProvider(SELECTOR, {
      async provideHover(document, position) {
        const range = document.getWordRangeAtPosition(position, /[A-Za-z$#@_\u00a3][\w$#@\u00a3]*/);
        if (!range) return null;
        const word = document.getText(range).toLowerCase();
        let found;
        try {
          found = await symbols(document);
        } catch (error) {
          return null;
        }
        const symbol = found.find((s) => s.name.toLowerCase() === word);
        return symbol ? new vscode.Hover(documentation(symbol), range) : null;
      }
    })
  );

  subscribe(
    vscode.languages.registerSignatureHelpProvider(
      SELECTOR,
      {
        async provideSignatureHelp(document, position) {
          const before = document.getText(
            new vscode.Range(new vscode.Position(position.line, 0), position)
          );
          const call = /([A-Za-z$#@_\u00a3][\w$#@\u00a3]*)\s*\(([^()]*)$/.exec(before);
          if (!call) return null;
          let found;
          try {
            found = await symbols(document);
          } catch (error) {
            return null;
          }
          const symbol = found.find(
            (s) => s.name.toLowerCase() === call[1].toLowerCase() && s.params && s.params.length
          );
          if (!symbol) return null;

          const parameters = symbol.params.map(
            (parameter) =>
              new vscode.ParameterInformation(
                `${parameter.name} ${parameter.type}`.trim(),
                parameter.doc || undefined
              )
          );
          const information = new vscode.SignatureInformation(
            `${symbol.name}(${symbol.params.map((p) => p.name).join(' : ')})`,
            documentation(symbol)
          );
          information.parameters = parameters;

          const help = new vscode.SignatureHelp();
          help.signatures = [information];
          help.activeSignature = 0;
          help.activeParameter = Math.min(
            call[2].split(':').length - 1,
            Math.max(0, parameters.length - 1)
          );
          return help;
        }
      },
      '(',
      ':'
    )
  );

  /* Expose Outline, breadcrumbs and symbol navigation only when this provider is enabled. */
  subscribe(
    vscode.languages.registerDocumentSymbolProvider(
      SELECTOR,
      {
        async provideDocumentSymbols(document) {
          if (!outlineEnabled()) return [];
          try {
            return outlineTree(document, await outlineSymbols(document));
          } catch (error) {
            return [];
          }
        }
      },
      { label: 'ibmicli' }
    )
  );

  /* Read-only remote member documents for comparisons. */
  subscribe(
    vscode.workspace.registerTextDocumentContentProvider(REMOTE_SCHEME, {
      onDidChange: remoteChanged.event,
      provideTextDocumentContent: (uri) => remoteTexts.get(uri.toString()) || ''
    })
  );

  /* ------------------------------------------------------------- Commands -- */

  /* Diff and SQL require local files; the active diff-side document can instead be an in-memory member. */
  const onLocalFile = (run) => async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;
    if (editor.document.uri.scheme !== 'file') {
      vscode.window.showInformationMessage('ibmicli: open a local file in the editor.');
      return;
    }
    try {
      await run(editor);
    } catch (error) {
      vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
    }
  };
  subscribe(
    vscode.commands.registerCommand('ibmicli.diffRemote', onLocalFile((editor) => diffRemote(editor.document)))
  );
  subscribe(
    vscode.commands.registerCommand('ibmicli.runSql', onLocalFile((editor) => runSql(editor, context)))
  );

  /* The copy link supplies file and line; palette and shortcut invocations use the current cursor. */
  subscribe(
    vscode.commands.registerCommand('ibmicli.openCopy', async (argument) => {
      let document;
      let line;

      if (argument && argument.file) {
        document = await vscode.workspace.openTextDocument(vscode.Uri.file(argument.file));
        line = document.lineAt(Math.max(0, argument.line - 1)).text;
      } else {
        const editor = vscode.window.activeTextEditor;
        if (!editor) return;
        document = editor.document;
        line = copyAt(document, editor.selection.active);
        if (!line) {
          vscode.window.showInformationMessage('ibmicli: the cursor is not on a /copy line.');
          return;
        }
      }

      try {
        const uri = await openCopy(document, line);
        if (!uri) return; // Download declined; no error to report.
        await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), {
          preview: false
        });
      } catch (error) {
        vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
      }
    })
  );

  subscribe(
    vscode.commands.registerCommand('ibmicli.pullCopies', async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const document = editor.document;
      let found;
      try {
        found = await copies(document);
      } catch (error) {
        vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
        return;
      }
      const missing = found.filter((copy) => !copy.error && !copy.exists);
      if (!missing.length) {
        vscode.window.showInformationMessage('ibmicli: all copies are already available locally.');
        return;
      }

      /* Confirm a whole group of missing copies once rather than asking separately for every member. */
      if (vscode.workspace.getConfiguration('ibmicli').get('confirmPull')) {
        const answer = await vscode.window.showInformationMessage(
          `Download ${missing.length} missing copies?`,
          { modal: true, detail: missing.map((copy) => `${copy.remote}\n  ${copy.path}`).join('\n\n') },
          'Download'
        );
        if (answer !== 'Download') return;
      }

      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'ibmicli: downloading copies' },
        async (progress) => {
          for (const copy of missing) {
            progress.report({ message: copy.remote });
            try {
              await openCopy(document, `/copy ${copy.copy}`, { silent: true });
            } catch (error) {
              CHANNEL.appendLine(`${copy.remote}: ${error.message}`);
            }
          }
        }
      );
      generation += 1;
      vscode.window.showInformationMessage(`ibmicli: ${missing.length} copies processed.`);
    })
  );

  /* F6 presents user-defined tasks applicable to the open file. */
  subscribe(
    vscode.commands.registerCommand('ibmicli.runTask', async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const document = editor.document;
      const name = path.basename(document.uri.fsPath);

      let found;
      try {
        found = await tasksFor(document);
      } catch (error) {
        vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
        return;
      }
      /* Even an empty task list keeps debug actions available. */
      const placeHolder = found.length
        ? `ibmicli on ${name}`
        : `ibmicli on ${name} (no tasks: define them in configuration; 'ibmicli tasks')`;
      const picked = await vscode.window.showQuickPick(
        taskItems(found, context.workspaceState.get(LAST_TASK), document),
        { placeHolder: placeHolder, matchOnDescription: true }
      );
      if (!picked) return;
      await context.workspaceState.update(LAST_TASK, picked.key);

      if (picked.command) {
        await vscode.commands.executeCommand(picked.command);
        return;
      }

      /* Save before running tasks so deploy cannot silently upload an older on-disk version. */
      if (
        document.isDirty &&
        vscode.workspace.getConfiguration('ibmicli').get('saveBeforeRun')
      ) {
        await document.save();
      }

      runInTerminal(picked.task.name, document);
    })
  );

  /* Debug uses the remote object *SOURCE view; breakpoints belong there. */
  const debugCommand = (run) => async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || !hasDebugger()) return;
    try {
      await run(editor.document);
    } catch (error) {
      vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
    }
  };
  subscribe(vscode.commands.registerCommand('ibmicli.debugBatch', debugCommand(debugBatch)));
  subscribe(
    vscode.commands.registerCommand(
      'ibmicli.debugSep',
      debugCommand((document) => debugSep(document, context))
    )
  );

  subscribe(
    vscode.commands.registerCommand('ibmicli.deployBuildChanged', async () => {
      const editor = vscode.window.activeTextEditor;
      const folders = vscode.workspace.workspaceFolders || [];
      /* With no open file, use the first workspace folder. */
      const document = editor
        ? editor.document
        : folders.length ? { uri: folders[0].uri } : null;
      if (!document) return;
      try {
        await deployBuildChanged(document);
      } catch (error) {
        vscode.window.showErrorMessage(`ibmicli: ${error.message}`);
      }
    })
  );

  subscribe(vscode.commands.registerCommand('ibmicli.clearProblems', clearProblems));

  subscribe(
    vscode.commands.registerCommand('ibmicli.refresh', () => {
      generation += 1;
      copyCache.clear();
      symbolCache.clear();
      vscode.window.showInformationMessage('ibmicli: symbols refreshed.');
    })
  );

  /* --------------------------------------------------------------- Events -- */

  subscribe(
    vscode.workspace.onDidSaveTextDocument(() => {
      /* Saving a copy changes symbols in every dependent source; invalidate all symbol caches. */
      generation += 1;
      copyCache.clear();
      symbolCache.clear();
    })
  );

  subscribe(
    vscode.workspace.onDidCloseTextDocument((document) => {
      if (document.uri.scheme === REMOTE_SCHEME) {
        remoteTexts.delete(document.uri.toString());
        return;
      }
      copyCache.delete(document.uri.fsPath);
      symbolCache.delete(document.uri.fsPath);
      outlineCache.delete(document.uri.fsPath);
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
