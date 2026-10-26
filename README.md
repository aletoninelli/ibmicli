# ibmicli

ibmicli is a Python CLI for working with IBM i source members as local files.
It uploads and downloads members over FTP, runs your CL build commands, and
connects local RPG sources to VS Code. Install it once and use it from any
folder.

## Why ibmicli

ibmicli is for sources that live in QSYS members and are compiled from
there. It mirrors the source tree as an ordinary local repository,
`APP/LIBRARY/SOURCEFILE/MEMBER.ext`, one file per member, under git, and makes
every operation on it a single command: pull, deploy, compile, compare,
query. It talks to IBM i over FTP: no SSH, nothing to install on the server,
and the code is compiled and run exactly as before.

Because it is a command-line tool first, two very different users get the
most from it: developers working in VS Code, and AI agents working in a
terminal.

### In VS Code

`ibmicli vscode install` builds and installs a VS Code extension from the CLI
itself, with no npm and no extra setup. With the whole codebase local, it
adds:

- **`/copy` and `/include` work.** Ctrl+click or F12 on a directive opens the
  local file, even across apps and libraries (`/copy GRP_SRC/QCPYSRC,UTILS`
  from a source in another app). A copybook that is not local yet is
  downloaded on request into the right folder. Copies without a library follow
  the environment's library list, as the compiler does.
- **Completion, hover, go to definition and signatures** for free-format RPG
  see every declaration: procedures, prototypes, data structures and their subfields,
  constants, subroutines. They follow copies recursively, so a prototype
  declared three copybooks deep is one keystroke away. Names containing `£`
  are fully supported.
- **Outline** of procedures, data structures and subroutines, updated as you
  type.
- **Compile with F6.** Your own compile tasks run as VS Code tasks. Compiler
  errors are read from the event file and mapped back to your local file,
  line and column, even through SQL precompilation and inside copybooks. They
  land in the Problems panel; a click takes you to the line.
- **Compare with the server.** See the difference between your file and the
  member on IBM i in VS Code's diff view, without writing a file.
- **Run SQL from the source.** Put the cursor on an `exec sql`, press F6, and
  ibmicli extracts the statement, asks for host-variable values and shows the
  results beside the editor. Read-only.
- **Debug** batch programs and service entry points (with user and condition)
  through IBM's debug extension, configured from the file you are editing.
- **Build what changed.** git knows which members you touched; one command
  deploys and compiles exactly those, as `*PGM` or `*SRVPGM`. From F6 it
  shows you the plan and waits for confirmation. Dependencies are not
  tracked: a changed copybook does not rebuild the programs that include it.

And because sources are plain files in git, you also get branches, history,
blame, code review, merge requests and full-text search across thousands of
members.

### A CLI built for AI agents

Without VS Code, ibmicli gives AI coding agents a way into IBM i. An agent
such as Claude Code, Codex or Copilot cannot open a 5250
screen or click through a remote member browser. It can read files, run
commands and read their output. ibmicli turns every IBM i action into exactly
that:

| The agent wants to... | Command |
| --- | --- |
| Get the sources | `ibmicli pull -lib LIB -file QRPGLESRC -all` |
| Understand a program and what it includes | `ibmicli copy -from FILE -all --json`, `ibmicli symbols FILE --json` |
| Edit | Local files, with the agent's own tools |
| See what it changed compared with IBM i | `ibmicli diff FILE`, `ibmicli status` |
| Save the member | `ibmicli deploy -src FILE` |
| Compile and fix the errors | `ibmicli run compile -src FILE`, which prints `path:line:col` and the message |
| Look at the data in Db2 for i | `ibmicli db "select ..." --json` |
| Test the SQL embedded in a program | `ibmicli db -from FILE -line N --var NAME=VALUE --json` |

A typical session needs no human in the loop until the review:

```sh
cd ORD                                   # the app folder
ibmicli pull -lib ORD_SRC -file QRPGLESRC -mbr ORDCHK -proj ../ibmiproj.toml
ibmicli copy -from ORD_SRC/QRPGLESRC/ORDCHK.sqlrpgle -all -pull -proj ../ibmiproj.toml
ibmicli symbols ORD_SRC/QRPGLESRC/ORDCHK.sqlrpgle --json -proj ../ibmiproj.toml
ibmicli db "select * from ordhdr fetch first 5 rows only" --json -proj ../ibmiproj.toml
# ...the agent edits the file...
ibmicli diff ORD_SRC/QRPGLESRC/ORDCHK.sqlrpgle -proj ../ibmiproj.toml
ibmicli run compile -src ORD_SRC/QRPGLESRC/ORDCHK.sqlrpgle -proj ../ibmiproj.toml
# ORD_SRC/QRPGLESRC/ORDCHK.sqlrpgle:142:9
#   RNF7030 severe error (30)  The name or indicator WKTOT is not defined.
# ...the agent fixes line 142 and compiles again...
```

The design choices that make this work for agents are deliberate:

- **Output made to be read by a program.** `--json` writes only JSON to
  stdout. Exit codes are consistent: `0` done, `1` operation failed, `2` usage
  or configuration error. Compiler errors use the `file:line:col` form every
  tool understands.
- **Nothing is guessed.** Library, source file, environment and object library
  come from `ibmiproj.toml` or from explicit options. When something is
  missing, the error says what to supply, so an agent can correct itself
  instead of compiling in the wrong library.
- **Safe by default.** Every command that touches IBM i has `-n` to show what
  it would do without connecting. `db` opens a read-only connection; changes
  need `--write`, and `--write --tx` keeps them in a transaction that is
  rolled back unless `commit` is typed. This lets you grant an agent `ibmicli
  db` freely and require confirmation for `ibmicli db --write`, `deploy` and
  `run` in its permission settings.
- **Context, not just text.** `copy` and `symbols` give the agent what a
  human gets from F12: where a prototype is declared, what a data structure
  contains, which copybook a constant comes from.

A platform whose code an agent cannot reach risks being left behind. ibmicli puts decades of RPG, CL and SQL within reach of
the same tools that already work on every other codebase, without changing
how the code is compiled or run on IBM i.

### Scope and limits

FTP handles sources and CL commands. Only `db` uses ODBC. Compilation commands
are tasks you define in configuration; ibmicli supplies no build recipes.
It does not replace a 5250 session or the IBM debugger, and its RPG symbol
reader is not a compiler.

ibmicli is young, and you should know where it stands:

- It has been used on one system (IBM i 7.5) from macOS. Windows and Linux
  are untested, and there is no automated test suite yet.
- FTP is not encrypted (FTPS is not supported), and the password is stored in
  plain text in `config.toml`, readable only by you (mode 600).
- Uploads go through `remote_charset`, ISO-8859-1 by default. A character it
  cannot represent, such as `€`, is replaced with `?` and reported.
- Building changed sources assumes one module per object and does not follow
  dependencies.
- The configuration format is recent and may still change.

Build output goes to the terminal. Comparisons and remote documents stay in
memory; no build logs or diff files are created locally. `db --out FILE`
explicitly exports query results. Downloads write the requested source files,
setup commands write configuration, and interactive prompts keep history in
the user's configuration directory. `show -edit` and extension installation
use temporary files.

## Installation and updates

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/). The packaged
Python dependencies are installed by uv. FTP access needs no ODBC driver.
For `db`, install the IBM i Access ODBC driver separately on each machine;
`ibmicli db --check` reports missing components and platform-specific remedies.

```sh
uv tool install git+https://github.com/aletoninelli/ibmicli.git
ibmicli --version
ibmicli set-conn
```

Make sure the uv tool executable directory, usually `~/.local/bin`, is in PATH.
Update the CLI and its bundled VS Code extension with:

```sh
uv tool upgrade ibmicli
ibmicli vscode install
```

To force a fresh installation from Git:

```sh
uv tool install --force --reinstall git+https://github.com/aletoninelli/ibmicli.git
```

For development, run these from a checkout:

```sh
uv sync
uv tool install --editable .
./deploy-local
```

An editable install picks up Python source edits immediately. Run
`./deploy-local` after changing dependencies or entry points in
`pyproject.toml`. Reinstall the VS Code extension after editing its sources.

## Global configuration and environments

`ibmicli set-conn` prompts for host, user, password and optional CCSID and debug
settings. It tests connections unless `--no-test` is supplied, then saves
`~/.config/ibmicli/config.toml` with mode 600. Credentials belong only here.
`XDG_CONFIG_HOME` changes the configuration directory; `IBMICLI_CONFIG` selects
a specific file, useful for isolated tests.

An environment describes the job library list. It can run your setup CL:

```toml
[envs.dev]
setup = ["CALL TOOLS/ENVPGM"]
curlib = "MYAPP_OBJ"
libl = ["MYAPP_SRC", "MYAPP_DAT", "COMMON_SRC", "TOOLS"]
```

With `setup`, `env set` and `env refresh` run those commands and read the
resulting `curlib` and user `libl` from IBM i. These cached values also support
offline plans, debug settings and copy lookup. Refresh after changing the
remote environment program.

Alternatively, omit `setup` and supply the list yourself:

```toml
[envs.manual]
curlib = "MYAPP_OBJ"
libl = ["MYAPP_SRC", "MYAPP_DAT"]
```

ibmicli generates `CHGLIBL` or `CHGCURLIB` for this form. The current library
is the default object destination unless an app sets `objlib`. Empty, QGPL
and `*CRTDFT` current libraries are refused as implicit build destinations.
Unqualified copies search the current library first, then the user library
list in order. Qualified names supply their own library.

```sh
ibmicli env set dev
ibmicli env use dev
ibmicli env refresh dev
ibmicli env refresh -all
ibmicli env ls
```

`env use` persists the environment used by `db` and the shell prompt. Tasks
and debug use their project app's environment, or an explicit `run -var
env=NAME`; they do not fall back to the active environment. The first
configured environment becomes active automatically.

`ibmicli config` shows effective values and mappings without displaying the
password. Settings such as `tasks_key`, `debug_host` and `debug_batch` are
root TOML keys: put them before any `[table]`.

## One project, several apps

Keep one `ibmiproj.toml` at the repository root. It contains app declarations,
folder-name exceptions and repository tasks, without credentials.

```text
sources/
  ibmiproj.toml
  MYAPP/                         declared by [apps.MYAPP]
    MYAPP_SRC/                   source library
      QRPGLESRC/                 source physical file
        HELLO.sqlrpgle           member HELLO
      QCPYSRC/
        CONSTS.rpgle
  SHARED/                        declared by [apps.SHARED]
    COMMON_SRC/
      QPROTSRC/
        PROCS.rpgle
  docs/                          not an app
```

```toml
[apps.MYAPP]
env = "dev"
objlib = "MYAPP_OBJ"

[apps.SHARED]
env = ""
objlib = ""
```

`[apps.MYAPP]` declares the immediate child folder `MYAPP` as an app. For
`MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle`, the next two folders supply library
`MYAPP_SRC` and source file `QRPGLESRC`. The app supplies `env` and `objlib`.
An empty `objlib` uses its environment's current library. An app without an
environment can hold sources for reading and copy resolution.

Folder names normally match IBM i object names. Exceptions map local folder
to remote name. Global mappings apply to every app; app mappings override
them for the same local folder:

```toml
[libraries]
SOURCES = "MYAPP_SRC"

[srcfile]
RPG = "QRPGLESRC"

[apps.SHARED.libraries]
SOURCES = "COMMON_SRC"

[apps.SHARED.srcfile]
RPG = "QPROTSRC"
```

Here `MYAPP/SOURCES/RPG/HELLO.sqlrpgle` resolves to
`MYAPP_SRC/QRPGLESRC(HELLO)`, while `SHARED/SOURCES/RPG/PROCS.rpgle` resolves
to `COMMON_SRC/QPROTSRC(PROCS)`. Quote TOML folder keys containing spaces.

An undeclared folder supplies no app, library or source file, even if its
subfolders look like IBM i names. A file outside `LIBRARY/SOURCEFILE` also
needs explicit values. Unknown keys inside `[apps.NAME]` are errors; the
allowed keys are `env`, `objlib`, `srclib`, `libraries` and `srcfile`.

### Apps with a single source library

If an app uses only one source library, you can drop the library folder level.
Set `srclib` and put the source file folders straight inside the app:

```text
repository/
  ibmiproj.toml
  api/
    src/                         source file QRPGLESRC (mapped below)
      GIRI.sqlrpgle              member API_SRC/QRPGLESRC(GIRI)
```

```toml
[apps.API]
env = "api"
objlib = ""
srclib = "API_SRC"

[apps.API.srcfile]
src = "QRPGLESRC"
```

The folder right after the app is the source file and the library is always
`srclib`. Such an app has no room for other libraries, so `[apps.NAME.libraries]`
is an error there, and a `/copy` to another library (say `CGIDEV2/QRPGLESRC,X`)
needs an app of its own:

```bash
ibmicli set-project API -srclib API_SRC -proj ibmiproj.toml
ibmicli set-project CGIDEV2 -env - -srclib CGIDEV2 -proj ibmiproj.toml
cd api && ibmicli pull -lib API_SRC -file QRPGLESRC -all -proj ../ibmiproj.toml   # into api/src/
```

`-srclib -` turns the app back into one with library folders.

## The -proj rule

The CLI never searches for a project. Pass `-proj FILE`, naming the actual
TOML file, not its directory. VS Code finds the nearest `ibmiproj.toml` while
walking from the open file to the open workspace root and passes it for you.
Open the repository root so that search can reach the file.

For a source inside a declared app, `-proj` supplies its mapping and app
values. Outside the repository it supplies no source mapping: use
`-lib/-file` for transfers and comparisons, or `-var` for task placeholders.
Repository tasks can still be selected explicitly with `-proj`.

| Command | Without `-proj` | What `-proj` adds |
| --- | --- | --- |
| `deploy` | Requires `-lib` and `-file` | Source library and source file from the file's app path; no environment setup |
| `pull` | Use `-path` or `-lib/-file`; default destination is `LIBRARY/SOURCEFILE` under the current location | App folder exceptions; inside a source-file folder, omission of `-lib/-file` |
| `list` | Supply `-lib` and `-f` | Current project folder's library and source file; a library folder can supply `-lib` |
| `diff` | Supply `-lib` and `-file` | Member mapping from each local path |
| `run` | Global tasks and explicit `-var` values | App environment/setup, object/current library, source names and repository tasks |
| `tasks` | Global tasks | Repository tasks, overriding global names |
| `copy`, `symbols` | No project copy resolution; `symbols -local` still reads declarations | Copy locations across apps and the source environment's search list |
| `db` | Active environment, or explicit `-lib` | Environment of the source/current folder's app and its object library |
| `status` | Requires `-proj` | Declared apps and known source-file folders to compare |
| `deploy-build-changed` | Requires `-proj` | Source classification and build context |
| `debug` | Requires `-proj` | App environment and object library |
| `show`, `env`, `set-conn`, `config`, `vscode`, `shell` | Work without a project | No `-proj` option |
| `set-project` | Requires an explicit destination file | Creates or updates the selected project |

Without `-o`, pull always retains the library/source-file structure. Inside a
project app it places files under that app, with its mappings. Elsewhere it
uses the current location, reusing a matching library/source-file folder
rather than nesting it again. It does not choose another app as a download
destination. Copy resolution separately searches all declared apps to locate
a referenced library.

## Start from an empty folder

First configure a `dev` environment as above. Create a repository folder and
declare two apps; these commands create the TOML, not the app directories:

```sh
mkdir sources
cd sources
ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml
ibmicli set-project SHARED -env - -objlib - -proj ibmiproj.toml
```

List every member of every source file in a library:

```sh
ibmicli list -lib MYAPP_SRC -f '*ALL'
```

Each output line is a `LIBRARY/FILE,MEMBER` path. Inspect the download of a
member before running it:

```sh
ibmicli pull -path MYAPP_SRC/QRPGLESRC,HELLO -n
```

To populate an app with every listed member, create and enter the app folder
first. The project is passed as a file, relative to that location:

```sh
mkdir MYAPP
cd MYAPP
ibmicli list -lib MYAPP_SRC -f '*ALL' | while IFS= read -r member; do
  ibmicli pull -path "$member" -proj ../ibmiproj.toml -n
done
cd ..
```

Review the `DRY` destinations, then remove `-n` to perform those downloads.
Repeat for shared sources using `SHARED` and `COMMON_SRC`.

Resolve missing copies before requesting downloads:

```sh
ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -pull -proj ibmiproj.toml -n
```

Run the same command without `-n` to download them. Then install the extension
and open the repository root in VS Code:

```sh
ibmicli vscode install
code .
```

Add the `compile` task from the task section below to `ibmiproj.toml`.
Inspect it before executing:

```sh
ibmicli tasks compile -proj ibmiproj.toml
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
```

The plan prints an upload, environment setup and compilation. After review,
run without `-n`, or press F6 in VS Code and select `compile`.

## Command reference

Use `ibmicli --help` or `ibmicli COMMAND --help` for all options. Configuration
and usage errors exit with 2. Successful operations exit with 0; operational
failures generally exit with 1. The exceptions and comparison meanings are
listed below. `--json` keeps stdout machine-readable; diagnostics use stderr.

Examples below assume the `sources` root, the layout above, a configured
`dev` environment, and the tasks shown later. Transfer and build examples
use `-n` to display their plans. Remove it to perform the operation.
Commands without `-n` are either read operations or explicit setup actions.

### deploy

Upload local files to source members. Choose exactly one of `-src FILE`,
`-srcs FILE1,FILE2` or `-all` (visible files in the current folder). A member
is the uppercase filename stem, limited to ten characters. SRCTYPE defaults
to the uppercase extension; `-t TYPE` overrides it. Explicit `-lib/-file`
win over project mapping. Existing remote members are overwritten.

```sh
ibmicli deploy -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
ibmicli deploy -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -lib MYAPP_SRC -file QRPGLESRC -t SQLRPGLE -n
```

Prints `DRY`, `OK`, `SKIP` or `FAIL` with local and remote names. Exit 0 for all
transferred, 1 if any file was skipped or failed.

### pull

Download members. Choose `-mbr`, comma-separated `-mbrs`, or `-all`; alternatively
use `-path LIBRARY/FILE,MEMBER`. `-o FOLDER` sets the exact destination.
Extensions come from `[pull_ext]` or the source-file defaults, unless included
in a requested member name. Existing local files are skipped unless `-f`.

```sh
ibmicli pull -path MYAPP_SRC/QRPGLESRC,HELLO -n
ibmicli pull -lib MYAPP_SRC -file QRPGLESRC -mbr HELLO -o MYAPP/MYAPP_SRC/QRPGLESRC -f -n
ibmicli pull -lib MYAPP_SRC -file QRPGLESRC -all -n
```

Prints remote-to-local result lines. Exit 0 if all requested transfers succeed,
1 for skipped or failed transfers. With explicit members, dry-run is offline;
`-all -n` still reads the remote member list before showing destinations.

### list

List a source file's members, or all files in a library with `-f '*ALL'`.
Inside a project source-file folder, `-proj` lets you omit `-lib/-f`.
This command is read-only and has no dry-run option.

```sh
ibmicli list -lib MYAPP_SRC -f QRPGLESRC
ibmicli list -lib MYAPP_SRC -f '*ALL'
```

Writes one `LIBRARY/FILE,MEMBER` per stdout line. Empty lists can exit 0.
For `*ALL`, unreadable files are skipped; exit 1 means every listed file
failed, or the library list could not be read.

### show

Read members to stdout with no headings. Accepts comma or parenthesized
member syntax. `-edit nano` and `-edit vsc` open temporary read-only copies.
There is no dry-run option.

```sh
ibmicli show MYAPP_SRC/QRPGLESRC,HELLO
ibmicli show 'MYAPP_SRC/QRPGLESRC(HELLO)'
ibmicli show MYAPP_SRC/QRPGLESRC,HELLO -edit vsc
```

Exit 0 when all members are read, 1 for failed reads or editor failures.
Unsupported editor names exit 2.

### diff

Compare files in memory as an upload would encode them. Trailing blanks and
empty final lines are ignored. Remote lines are `-`, local lines `+`.
`-U N` sets context; `--json` returns member content and comparison results.

```sh
ibmicli diff MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
ibmicli diff MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -lib MYAPP_SRC -file QRPGLESRC -U 5 -n
```

Prints a unified diff or `OK` for equal content. Exit 0 means all equal;
1 means different, missing or unreadable. Dry-run only prints member mappings.

### status

Requires `-proj`. Compare known source-file folders below a source-file folder,
library, app or repository root. `-remote` lists remote-only members; `-v`
also shows equal files.

```sh
ibmicli status -proj ibmiproj.toml -n
ibmicli status MYAPP/MYAPP_SRC/QRPGLESRC -remote -v -proj ibmiproj.toml -n
```

Prints `DIFF` with added/removed counts, `LOCAL` for local-only files and,
with `-remote`, `IBMI` for remote-only members, followed by a summary.
Exit 1 means local differences, missing remote counterparts or read failures.
Remote-only members alone do not make status fail. Dry-run lists folders.

### copy

Resolve directives across project apps. `-from SOURCE` supplies context.
A single directive downloads by default; `-all` requires explicit `-pull`.
`-no-pull` reads only; `-open` opens resolved files in VS Code.

```sh
ibmicli copy MYAPP_SRC/QCPYSRC,CONSTS -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -no-pull -proj ibmiproj.toml -n
ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -pull -proj ibmiproj.toml -n
```

Prints resolved paths or reasons for missing destinations. `--json` returns
copy locations and errors. Exit 1 for unresolved or missing copies; missing
files planned in dry-run do not count as transfer failures.

### symbols

Read free-format RPG declarations, following readable copies recursively.
`-copies` excludes root declarations; `-local` excludes copies. `-k KIND`
filters declaration kind. `--stdin` reads the root text from an editor buffer.
This command never transfers files and has no dry-run option.

```sh
ibmicli symbols MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
ibmicli symbols MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -local --json
```

Prints kind, name, type and source location, or JSON with `symbols` and
`problems`. Unreadable copies do not hide other symbols. Text mode exits 1
when copies have problems; JSON mode returns 0 with those problems in its
payload. Invalid option combinations or a missing root file exit 2.

### run

Run only explicitly configured tasks, through `run NAME`. `-src FILE` is
required only when the steps use source values. `-var NAME=VALUE` overrides
project values; multiple pairs can follow it. `-v` displays complete CL output.

```sh
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -var srclib=MYAPP_SRC srcfile=QRPGLESRC objlib=MYAPP_OBJ env=dev -n
```

Prints a task heading, expanded steps and final result. Stops on the first
failed step and exits 1; invalid tasks or missing values exit 2. Dry-run prints
CLI and local shell steps without starting them.

### tasks

List configured tasks in definition order. A task name shows its steps;
`-e EXT` filters by source extension. `--guide` explains the complete syntax.

```sh
ibmicli tasks -e sqlrpgle -proj ibmiproj.toml
ibmicli tasks compile -proj ibmiproj.toml
ibmicli tasks --guide
```

Prints names, labels, filters and definition problems. `--json` supplies the
extension menu. An empty text list exits 1; an empty JSON list exits 0.
Unknown task names exit 2. Editing a task requires no CLI or extension rebuild.

### deploy-build-changed

Requires `-proj` and Git. Includes changes from HEAD and untracked files below
the current folder. `--since REF` uses the merge base and includes committed
changes since branching. `--json` returns a plan without running it.

```sh
ibmicli deploy-build-changed -proj ibmiproj.toml -n
ibmicli deploy-build-changed --since HEAD -proj ibmiproj.toml -n
ibmicli deploy-build-changed --json -proj ibmiproj.toml
```

Uploads all selected sources before building service programs, then programs.
Build task names come from `[deploy-build-changed]` in global configuration.
Unmapped source files upload only. NOMAIN distinguishes service programs
assuming one module per object. Deleted local files leave remote members
untouched. Failed uploads are not compiled; other files continue.

Prints the plan, ignored/skipped paths and results. Exit 1 for skipped or
failed files; 0 for success or no changes. The JSON plan itself exits 0 and
includes skipped paths. Until changes are committed, rerunning uploads them
again; there is no upload ledger.

### debug

Requires `-proj`. Prepare offline launch settings for a source program.
`-sep` omits the batch submission command; `--json` includes credentials for
the extension. Never redirect or log that launch JSON.

```sh
ibmicli debug MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
ibmicli debug MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -sep -proj ibmiproj.toml
```

Normal output shows environment, program, module, hostname and submission
command, without a password. The command does not connect and has no `-n`
option. Invalid configuration exits 2.

Configure `debug_host` as a TLS certificate hostname, such as
`ibmi.example.com`. `debug_batch` is CL with task placeholders, for example
`SBMJOB CMD(CALL PGM({objlib}/{member})) JOBQ(QSYS/QSYSNOMAX) MSGQ(*USRPRF)
CPYENVVAR(*YES)`. Both belong before any TOML table. The IBM extension performs
actual debugging. Compile with `DBGVIEW(*SOURCE)` or `*ALL`.

### db

Run SQL through ODBC. `-lib LIB1,LIB2` supplies a library list; otherwise use
the app environment with `-proj`, or the active environment. `--json` emits
one record per statement; `--csv` emits headers and data. `--out FILE`
overwrites the requested result file only after a successful query.

```sh
ibmicli db "select 1 from sysibm.sysdummy1" -n
ibmicli db -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -line 5 --json -proj ibmiproj.toml -n
ibmicli db --out result.csv "select 1 from sysibm.sysdummy1" -n
```

Without SQL, a terminal opens `sql>`; redirected stdin is read as one statement.
Direct SQL remains one statement so procedure bodies can contain semicolons.
With `-from`, supplied source text is split into statements; `-line N` selects
the statement at that source line. RPG extraction removes `exec sql` and
SELECT/VALUES host-variable INTO, extracts cursor queries, and turns
`set :host = expression` into `values expression`. Supply remaining host
variables as SQL with `--var NAME=VALUE`, including quotes for strings.

The connection is read-only by default. `--write` explicitly permits changes
and commits each statement immediately. `--write --tx` enables a transaction;
only `commit` in the prompt confirms changes. `rollback`, exit, Ctrl-D,
terminal closure and connection loss discard uncommitted work. A single
statement run with `--tx` also ends in rollback. Tables need journaling.

Enter adds a line; a final `;`, an empty final line or Esc+Enter executes the
block. Up recalls complete statements. TAB completes keywords and known
tables. The last 20 distinct statements are retained in `sql_history`.

No row limit is imposed; add `fetch first N rows only` to SQL when needed.
JSON makes duplicate columns unique (`NAME_2`), trims CHAR padding and keeps
inexact decimal-to-float conversions as strings. CSV uses empty fields for
NULL. Text output includes a row count on stderr.

Exit 1 for connection, statement or export failures; usage and missing driver
configuration exit 2. The interactive prompt reports individual query errors
and continues; normal prompt exit returns 0. `db --check` reports installation
and connectivity and exits 1 for failed checks.

### env

Manage environments with `set`, `use`, `refresh` and `ls`. `set --no-test`
skips remote testing; `refresh -all` updates all cached lists. `-q` prints only
the active name for shell prompts.

```sh
ibmicli env ls
ibmicli env -q
ibmicli env use dev
```

Prints setup commands, `CUR` and `USR` libraries. Setup actions write global
configuration. Exit 1 for absent active environments, empty listings or
failed refreshes; missing names/configuration can exit 2.

### set-conn

Prompt for connection and optional debug settings. Existing environments,
tasks, mappings and the task shortcut are preserved. `-f` skips the replacement
confirmation; `--no-test` skips FTP, debug TLS and ODBC probes.

```sh
ibmicli set-conn --no-test
ibmicli set-conn -f --no-test
```

Writes global configuration with mode 600 and prints its path. Saves no
default tasks. Refusing replacement exits 0; refusing to save after a failed
FTP test exits 1. Missing ODBC components are explained without blocking FTP.

### set-project

Add or edit one app in the explicitly named project file. `-env NAME` selects
a configured environment; `-objlib LIBRARY` overrides its current library.
`-` clears either setting. With neither option, prompt for both values.

```sh
ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml
ibmicli set-project SHARED -env - -objlib - -proj ibmiproj.toml
```

Prints the app folder, environment and object destination. Creates a missing
TOML file, preserving other apps, mappings and tasks; handwritten comments
are replaced. It creates neither app directories nor VS Code task files.
Exit 0 on success, 2 for invalid names or configuration.

### config

Show effective global settings, defaults, environments and tasks, excluding
the password value. `-s` limits detail.

```sh
ibmicli config
ibmicli config -s
```

Prints connection/encoding information and configured mappings. Exit 0 for a
valid configuration, 2 if it cannot be loaded.

### vscode

Build and install the bundled extension. Required companions supply IBM i
languages and IBM debug. `-extra` also installs the RPGLE extension and Code
for IBM i; `-only` installs only ibmicli. `uninstall` leaves third-party
extensions installed. The companion extensions are described below.

```sh
ibmicli vscode status
ibmicli vscode install
ibmicli vscode install -only
```

Prints installed versions, task shortcut and binding conflicts. Reload VS Code
after installation. Status exits 1 when VS Code is absent; installation can
exit 1 for companion failures. Invalid actions and extension command failures
exit 2.

### shell

Start the interactive shell, also opened by `ibmicli` without arguments.
The same commands, options and help apply inside it; omit the `ibmicli` prefix.
The FTP session is shared until exit. Unknown command names, pipes and
redirections go to the local shell; `cd` updates the working folder.

```sh
ibmicli shell
ibmicli
```

The prompt shows the active environment. Use `help`, `help pull`, or
`pull --help`; `exit`, `quit` and Ctrl-D leave. Ctrl-C cancels the current
line. Command failures are reported without leaving the shell; normal shell
exit returns 0.

## Tasks and compiler diagnostics

Tasks live only in user files: global `[tasks.NAME]` in `config.toml`, or
repository `[tasks.NAME]` in the explicit `ibmiproj.toml`. Repository definitions
win when names match. A task is invoked only as `ibmicli run NAME`, including
when its name matches a CLI command.

Add this repository task for an upload followed by SQLRPGLE module compilation:

```toml
[tasks.compile]
label = "Compile SQLRPGLE module"
ext = ["sqlrpgle"]
env = true
steps = [
  "ibmicli deploy -src {file} -lib {srclib} -file {srcfile}",
  "CRTSQLRPGI OBJ({objlib}/{member}) SRCFILE({srclib}/{srcfile}) SRCMBR({member}) OBJTYPE(*MODULE) OPTION(*EVENTF) DBGVIEW(*SOURCE)",
]
```

`ext = []` accepts all extensions. `label` is optional. `env = true` sets up
the selected environment immediately before the first CL or `job:` step.
Only tasks that use source placeholders require `-src`. A task with no
environment-dependent values and no environment setup can run standalone.

| Step | Execution |
| --- | --- |
| `ibmicli ...` | The same CLI application; no second implementation of deploy or pull |
| `sh: ...` | Local shell in the task's starting folder |
| `job: ...` | RCMD in the persistent FTP job; library-list changes survive |
| `task: NAME` | Replace with that task's steps before execution |
| No prefix | CL in a QShell child job, returning status and messages |

An unprefixed `echo hello` goes to IBM i as CL; use `sh:` for local commands.
Included tasks use the outer task's source, environment and extension filter.
Inclusion cycles are refused with their chain. Steps stop at the first failure.

QShell inherits the FTP library list, but each CL child has its own QTEMP and
does not inherit file overrides. Use `job:` for persistent library-list setup;
normal CL for compilation and its messages. `job:` returns status only.

| Placeholder | Value |
| --- | --- |
| `{member}` | Uppercase member name without extension |
| `{file}` | Local path as supplied with `-src` |
| `{dir}` | Local source folder |
| `{name}` | Local filename including extension |
| `{ext}` | Local extension, lowercase |
| `{srctype}` | Member source type from the extension mapping |
| `{srcfile}` | Project source-file folder mapping |
| `{srclib}` | Project library folder mapping |
| `{env}` | App environment name |
| `{objlib}` | App object library, otherwise environment current library |
| `{curlib}` | Environment current library |

`-var NAME=VALUE` overrides these values. `env=dev` also selects setup and the
current library. Without a usable project, supply every value the task needs;
folder names alone and the active environment are not used as fallbacks.
CLI and shell placeholder values are quoted so paths with spaces remain intact.
A nested `ibmicli` step does not inherit `-proj` or `-var`; pass `-lib {srclib}
-file {srcfile}` to deploy and pull steps explicitly.

`OPTION(*EVENTF)` writes compiler diagnostics to `OBJLIB/EVFEVENT(MEMBER)`.
ibmicli uses a changed event timestamp to avoid stale errors, then maps source
and copy lines to `file:line:column`. SQL precompiler expansions are mapped
back to the original source. The terminal and VS Code Problems can open the
location. A copy absent locally is reported by its remote member name.
For CRTSQLRPGI, do not combine `OPTION(*EVENTF)` with an additional `OPTION`
inside `COMPILEOPT`; duplicate parameters prevent RPG compilation.

For bulk builds, define tasks that compile without a deploy step, then map
the source file in global configuration:

```toml
[deploy-build-changed]
QRPGLESRC = { pgm = "buildpgm", srv = "buildsrv" }
QCLLESRC = "buildcl"
```

A string selects the same task for all members of that source file. A table
selects `srv` for NOMAIN and `pgm` otherwise. Define the named tasks yourself;
missing task definitions are errors before any uploads.

## VS Code workflow

Install VS Code and run `ibmicli vscode install`, then reload its window.
Open the repository root. The bundled extension needs no npm tooling.

- F6 lists applicable tasks, debug commands, comparison, SQL and project-wide
  deploy/build. Bulk deploy/build requires confirmation of the CLI plan.
- Ctrl+click, F12 or Cmd/Ctrl+F12 on a copy opens its local file, confirming a
  download when missing. A separate command downloads all missing copies.
- Completion, hover, definition navigation and signatures include recursive
  copies and structure subfields.
- Outline follows unsaved text and defers to Halcyon's provider in `auto` mode.
- Batch and SEP debugging use `IBM.ibmidebug`. The password stays in memory;
  tracing is disabled. Breakpoints refer to the remote object's source view.
- Cursor or selected SQL runs read-only and displays results beside the editor.
- Member comparison uses a read-only document held in memory.

`tasks_key` in global configuration controls the menu shortcut, default F6.
Changing it requires `ibmicli vscode install`; changing tasks does not.
The installer reports conflicting extension bindings. Set
`ibmicli.executable` if the CLI cannot be found automatically.

### Companion extensions and Code for IBM i

| Extension | Installed by | Purpose |
| --- | --- | --- |
| IBM i Languages (`barrettotte.ibmi-languages`) | `ibmicli vscode install` | Syntax coloring for RPGLE, CLLE, DDS and CL |
| IBM i Debug (`IBM.ibmidebug`) | `ibmicli vscode install` | Batch and service-entry-point debugging |
| RPGLE (`halcyontechltd.vscode-rpgle`) | `ibmicli vscode install -extra` | RPGLE linter and language tools, including its own Outline |
| Code for IBM i (`halcyontechltd.code-for-ibmi`) | `ibmicli vscode install -extra` | Browse remote IBM i libraries and members from VS Code |

Code for IBM i uses a direct connection to IBM i. It is optional: ibmicli
works with local source files and transfers members through its own FTP
connection. The ibmicli extension and IBM i Debug work without Code for IBM i.

To install both optional Halcyon extensions as well as the standard companions:

```sh
ibmicli vscode install -extra
```

Run `ibmicli vscode status` to see which extensions are installed. Remove an
unused companion through VS Code; `ibmicli vscode uninstall` removes only
the ibmicli extension.

## Common problems

| Message or symptom | Action |
| --- | --- |
| `configuration not found` | Run `ibmicli set-conn` |
| `missing library and source file` | Supply `-lib LIBRARY -file SRCFILE`; for app files, `-proj FILE` supplies them |
| `task ... is missing {srclib}, ...` | Supply `-var` values; for app files, pass the repository TOML with `-proj` |
| `unknown keys` in an app | Use only `env`, `objlib`, `srclib`, `libraries`, `srcfile` in `[apps.NAME]` |
| `environment ... does not exist` | Define it with `env set NAME`, or correct the app's `env` |
| Unknown object library | Set app `objlib`, supply a task override, or refresh a valid environment current library |
| Copy missing locally | Inspect `copy -all -no-pull`, then use `copy -all -pull` with `-from` and `-proj` |
| Project works in terminal but not VS Code | Open the repository root; discovery stops at the workspace boundary |
| `driver ... is not installed`, pyodbc import failure | Run `db --check` and follow its OS-specific instructions; FTP remains usable |
| `db is read-only` | Use explicit `--write` for intended modifications; add `--tx` for a transaction |
| `SQL7008` with `--tx` | Journal the table, or use immediate writes without `--tx` if intended |
| Compiler errors have no source locations | Add `OPTION(*EVENTF)` to the build task; verify its object library/member |
| `CPD0042` before RPG compilation | Remove duplicate `OPTION` from `COMPILEOPT` |
| SEP TLS failure | Use a certificate-covered hostname in `debug_host`, not the FTP IP |
| F6 runs a different command | Resolve the reported keybinding conflict or change `tasks_key` and reinstall |
| Extension still uses old text or behavior | Reinstall it and run `Developer: Reload Window` |
| Accents or symbols arrive incorrectly | Check local UTF-8, remote character set and server CCSID; uploads use FTP line conversion |

## License

MIT. See [LICENSE](LICENSE).
