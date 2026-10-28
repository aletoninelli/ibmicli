# ibmicli for VS Code

Support for local IBM i source files, using the ibmicli CLI for transfers,
project mappings, symbols, tasks and SQL. Install and update with:

```sh
ibmicli vscode install
```

Reload the window with `Developer: Reload Window` after installation.
No npm installation or separate extension build is needed.

## Companion extensions and Code for IBM i

`ibmicli vscode install` also installs IBM i Languages
(`barrettotte.ibmi-languages`) for syntax coloring and IBM i Debug
(`IBM.ibmidebug`) for debugging.

`ibmicli vscode install -extra` additionally installs the RPGLE linter and
tools (`halcyontechltd.vscode-rpgle`) and Code for IBM i
(`halcyontechltd.code-for-ibmi`). Code for IBM i browses remote libraries and
members through a direct IBM i connection. It is optional: ibmicli uses local
source files and its own FTP connection, and IBM i Debug works without it.

Use `ibmicli vscode status` to inspect installed companions. `-only` installs
just the ibmicli extension; uninstalling ibmicli leaves companions installed.

## Project selection

Open the repository root containing `ibmiproj.toml`. The extension searches
upward from the source file to the open workspace folder, then passes the
nearest project file to the CLI as `-proj FILE`. It never searches above the
workspace root. Opening only an app subfolder hides a project file above it.

A source path looks like `MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle`.
`[apps.MYAPP]` supplies its environment and object library. The next two
folders supply the source library and source file.

## F6 tasks and compiler diagnostics

F6 lists tasks for the open file's extension. Define global tasks in
`~/.config/ibmicli/config.toml` or repository tasks in `ibmiproj.toml`.
`ibmicli tasks --guide` explains steps and placeholders. Task edits apply
immediately, without reinstalling the extension.

The last selected task or read-only file action appears first. Debug actions
and project-wide deploy/build are also available. Bulk deploy/build stays at
the end and requires confirmation of its plan.

Tasks use a shared terminal panel and the `$ibmicli` problem matcher.
Compilations with `OPTION(*EVENTF)` produce file and line diagnostics in
Problems, including copies available locally. Each run replaces the previous
run's diagnostics. Outside a workspace, results remain in the terminal.
To remove them without compiling again, use the clear button in the
Problems panel title bar, choose `clear problems` in the F6 menu, or run
`ibmicli: clear compiler problems` from the Command Palette.

The extension saves the source before running a task by default. Set
`ibmicli.saveBeforeRun` to false to disable this.

## Copies and completion

Ctrl+click, F12 or `ibmicli: open the /copy under the cursor` opens the local
file for a directive such as:

```rpgle
/copy MYAPP_SRC/QPROTSRC,PROCS
```

If missing, the extension shows the destination and asks before downloading.
Disable automatic downloading with `ibmicli.pullOnOpen`, or just the prompt
with `ibmicli.confirmPull`. The command `ibmicli: download missing copies`
handles all missing copies in the source with one confirmation.

Copy lookup uses all declared apps. Libraries absent from those apps get a
prospective folder inside the source app. Unqualified directives follow its
environment library list. Unresolved copies are explained when opened rather
than marked as compiler errors.

Completion, hover, F12 navigation and parameter hints include declarations
from recursive copies. Qualified structures and `likeds` templates supply
subfield suggestions. Parsing supports free-format RPG declarations.

## Outline

Procedures contain local declarations and subroutines; structures contain
subfields. Outline follows the current editor buffer. `ibmicli.outline` can be
`auto`, `on` or `off`. Automatic mode defers to the Halcyon RPGLE extension
when installed, avoiding duplicate entries.

## Member comparison and SQL

`ibmicli: compare the file with its IBM i member` opens a diff with the member
on the left and the local file on the right. The remote document is read-only
and stays in memory.

`ibmicli: run cursor or selected SQL` prepares a statement through the CLI.
In RPG, it removes `exec sql` and host-variable `INTO` clauses and extracts
cursor queries. Host-variable values are entered as SQL, including quotes.
Results appear beside the editor in a table. The connection is always
read-only, and the result webview runs no scripts.

## Debug

Batch debug submits the configured `debug_batch` command. Service entry
points ask for a procedure, triggering user and optional condition. Empty
procedure means the whole program. Remove a SEP from the IBM extension's
Service Entry Points view when finished.

The official `IBM.ibmidebug` extension performs debugging; `vscode install`
installs it as a companion. Compile with `DBGVIEW(*SOURCE)` or `*ALL`.
Use a `debug_host` hostname covered by the Debug Service TLS certificate,
not an IP address. The launch password stays in memory; it is not saved in
`launch.json`, settings or workspace state. Breakpoints use the remote
object's source view.

## Executable and settings

`ibmicli.executable` can specify the CLI path. Empty searches PATH,
`~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`.
`ibmicli.completion` enables copy symbol suggestions. The task shortcut comes
from `tasks_key` in global configuration; changing it requires reinstalling
the extension with `ibmicli vscode install`.
