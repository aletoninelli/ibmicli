# ibmicli

ibmicli is a Python CLI for working with IBM i source members as local files.
It downloads and uploads members over FTP, runs your own CL build commands, and
connects local RPG sources to VS Code. Install it once per machine and use it
from any folder.

- [Why ibmicli](#why-ibmicli)
- [How it fits together](#how-it-fits-together): connection, environments, project
- [Installation](#installation)
- [Getting started, step by step](#getting-started-step-by-step)
- [Configuration in detail](#configuration-in-detail)
- [Tasks and compiler errors](#tasks-and-compiler-errors)
- [VS Code](#vs-code)
- [Command reference](#command-reference)
- [Scope and limits](#scope-and-limits)
- [Common problems](#common-problems)

## Why ibmicli

ibmicli is for sources that live in QSYS members and are compiled from there.
It mirrors them as an ordinary local repository,
`APP/LIBRARY/SOURCEFILE/MEMBER.ext`, one file per member, under git, and makes
every operation a single command: pull, deploy, compile, compare, query. It
talks to IBM i over FTP: no SSH, nothing to install on the server, and the code
is compiled and run exactly as before.

Because sources are plain files in git, you also get branches, history, blame,
code review and full-text search across thousands of members.

### In VS Code

`ibmicli vscode install` builds and installs a VS Code extension from the CLI
itself, with no npm and no extra setup. It adds:

- **`/copy` and `/include` that work.** Ctrl+click or F12 opens the local
  file, even across apps and libraries. A copybook that is not local yet is
  downloaded on request into the right folder.
- **Completion, hover, go to definition and signatures** for free-format RPG,
  following copies recursively. Names containing `£` are supported.
- **Outline** of procedures, data structures and subroutines.
- **Compile with F6.** Your compile tasks run as VS Code tasks; compiler
  errors land in the Problems panel at the right line of your local file,
  even through SQL precompilation and inside copybooks.
- **Compare with the server**, in VS Code's diff view, without writing files.
- **Run the SQL under the cursor** (read-only) and see the results beside the
  editor.
- **Debug** batch programs and service entry points through IBM's debug
  extension.
- **Build what changed**: git knows which members you touched; one command
  deploys and compiles exactly those.

### A CLI built for AI agents

An AI coding agent (Claude Code, Codex, Copilot...) cannot open a 5250 screen,
but it can read files, run commands and read their output. ibmicli turns every
IBM i action into exactly that:

| The agent wants to... | Command |
| --- | --- |
| Get the sources | `ibmicli pull -lib LIB -file QRPGLESRC -all` |
| Understand a program and its copybooks | `ibmicli copy -from FILE -all --json`, `ibmicli symbols FILE --json` |
| See what it changed compared with IBM i | `ibmicli diff FILE`, `ibmicli status` |
| Save the member | `ibmicli deploy -src FILE` |
| Compile and fix the errors | `ibmicli run compile -src FILE` prints `path:line:col` and the message |
| Look at the data | `ibmicli db "select ..." --json` |
| Test the SQL embedded in a program | `ibmicli db -from FILE -line N --var NAME=VALUE --json` |

What makes this safe and reliable for agents:

- **Output for programs.** `--json` writes only JSON to stdout. Exit codes are
  consistent: `0` done, `1` operation failed, `2` usage or configuration error.
- **Nothing is guessed.** Library, source file, environment and object library
  come from `ibmiproj.toml` or from explicit options. When something is
  missing, the error says what to supply.
- **Safe by default.** Every command that touches IBM i has `-n` (dry run).
  `db` is read-only unless `--write` is given, so you can allow `ibmicli db`
  freely and require confirmation for `db --write`, `deploy` and `run`.

## How it fits together

There are three things to configure. Each lives in one place:

| What | Where | Written by | Scope |
| --- | --- | --- | --- |
| **Connection**: host, user, password, CCSID | `~/.config/ibmicli/config.toml` | `ibmicli set-conn` | this machine, private (mode 600) |
| **Environments**: how to set up a library list | same file, `[envs.NAME]` | `ibmicli env set` | this machine |
| **Project**: which folders are apps, and which environment and object library each app uses | `ibmiproj.toml` at the repository root | `ibmicli set-project` | the repository, committed to git, no credentials |

Your sources sit in the repository with this layout:

```text
sources/                                    repository root
  ibmiproj.toml                             the project file
  MYAPP/                                    an app, declared in ibmiproj.toml
    MYAPP_SRC/                              source library
      QRPGLESRC/                            source file
        HELLO.sqlrpgle                      member HELLO, type SQLRPGLE
```

From a file path, ibmicli knows everything it needs to deploy, compile or
debug it:

```text
MYAPP / MYAPP_SRC / QRPGLESRC / HELLO.sqlrpgle
  │        │           │          └─ member HELLO, source type SQLRPGLE
  │        │           └─ source file QRPGLESRC
  │        └─ source library MYAPP_SRC
  └─ app MYAPP ── ibmiproj.toml ──> env = "dev", objlib = "MYAPP_OBJ"
                                      │
                    config.toml ──> [envs.dev] library list, current library
```

Two rules follow from this:

1. **Commands that use the project need `-proj PATH/TO/ibmiproj.toml`.** The
   CLI never searches for the file. VS Code finds it for you.
2. **Nothing is guessed.** If a file is not inside a declared app, you supply
   the values yourself (`-lib`/`-file`, or `-var` for tasks).

## Installation

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/aletoninelli/ibmicli.git
ibmicli --version
```

Make sure the uv tool directory, usually `~/.local/bin`, is in your PATH.

FTP needs nothing else. Only `db` (SQL) needs the **IBM i Access ODBC driver**,
installed separately; `ibmicli db --check` tells you what is missing and how
to install it on your system.

To update the CLI and its VS Code extension:

```sh
uv tool upgrade ibmicli
ibmicli vscode install
```

## Getting started, step by step

This walkthrough starts from nothing and ends with a program compiled from
VS Code. The example uses:

| | |
| --- | --- |
| Source library | `MYAPP_SRC` (source files `QRPGLESRC`, `QCPYSRC`) |
| Object library | `MYAPP_OBJ` |
| Environment program | `CALL TOOLS/ENVPGM` sets the library list (optional) |
| Repository folder | `~/sources` |

Replace them with your own names. Commands that change something accept `-n`
to show what they would do first.

### Step 1: connect to IBM i

```sh
ibmicli set-conn
```

It asks for host, user and password (plus optional CCSID and debug settings,
which you can skip with Enter), tests the FTP connection, and saves
`~/.config/ibmicli/config.toml`. You do this once per machine.

### Step 2: create an environment

An environment says **how to set up the library list** of the job that
compiles your code. Create one called `dev`:

```sh
ibmicli env set dev
```

It asks which of two ways you use:

- **`cl`**: you already have a program or command that sets up the library
  list (for example `CALL TOOLS/ENVPGM`). Type it; ibmicli runs it on IBM i
  and reads back the resulting current library and library list.
- **`list`**: you type the current library and the library list yourself.

```text
$ ibmicli env set dev
New environment 'dev'. How is the library list set?
  cl     with your CL commands, e.g. CALL of an environment program:
         ibmicli reads the resulting library list from IBM i
  list   by supplying current library and user library list
Mode (cl/list) [cl]: cl
Environment setup CL, one command per line; an empty line ends the list.
  CL 1: CALL TOOLS/ENVPGM
  CL 2:
testing environment 'dev' on 10.0.0.5
OK    environment 'dev': current library MYAPP_OBJ, 3 user libraries
OK    environment 'dev' saved to /Users/me/.config/ibmicli/config.toml
```

Check it with `ibmicli env ls`. If you later change the environment program
on IBM i, run `ibmicli env refresh dev`.

### Step 3: create the project

Create the repository folder and declare the app. This writes `ibmiproj.toml`;
it does not create any other folder.

```sh
mkdir ~/sources
cd ~/sources
git init
ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml
```

The file now contains:

```toml
[apps.MYAPP]
env = "dev"
objlib = "MYAPP_OBJ"
```

`objlib` is where programs are created. With `-objlib -` (empty) the
environment's current library is used.

### Step 4: download the sources

See what is in the library:

```sh
ibmicli list -lib MYAPP_SRC -f '*ALL'
```

Then download from **inside the app folder**. That is how pull knows the files
belong to `MYAPP`; run from the repository root, it would put them in
`~/sources/MYAPP_SRC/...` instead.

```sh
mkdir MYAPP
cd MYAPP
ibmicli pull -lib MYAPP_SRC -file QRPGLESRC -all -proj ../ibmiproj.toml
ibmicli pull -lib MYAPP_SRC -file QCPYSRC -all -proj ../ibmiproj.toml
cd ..
```

```text
folder /Users/me/sources/MYAPP/MYAPP_SRC/QRPGLESRC
OK    MYAPP_SRC/QRPGLESRC(HELLO)  ->  /Users/me/sources/MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle
...
```

If a program includes copybooks from other libraries, download them too. They
go into the folder where the compiler would find them:

```sh
ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -pull -proj ibmiproj.toml
```

Commit the result: `git add . && git commit -m "Sources from IBM i"`.

### Step 5: add a compile task

ibmicli has no built-in compile commands: you write them, as **tasks**. Add
this to `ibmiproj.toml`:

```toml
[tasks.compile]
label = "Upload and compile"
ext = ["sqlrpgle"]
env = true
steps = [
  "ibmicli deploy -src {file} -lib {srclib} -file {srcfile}",
  "CRTSQLRPGI OBJ({objlib}/{member}) SRCFILE({srclib}/{srcfile}) SRCMBR({member}) OBJTYPE(*PGM) OPTION(*EVENTF) DBGVIEW(*SOURCE)",
]
```

It uploads the file, sets up the `dev` environment (`env = true`), then
compiles. The `{...}` placeholders are filled from the file path and the
project. `OPTION(*EVENTF)` is what lets ibmicli show compiler errors at the
right line.

Check what it would do, without connecting:

```sh
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
```

```text
task compile  env dev  member HELLO  ->  MYAPP_OBJ
> ibmicli deploy -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -lib MYAPP_SRC -file QRPGLESRC
> CALL TOOLS/ENVPGM
> CRTSQLRPGI OBJ(MYAPP_OBJ/HELLO) SRCFILE(MYAPP_SRC/QRPGLESRC) SRCMBR(HELLO) OBJTYPE(*PGM) OPTION(*EVENTF) DBGVIEW(*SOURCE)
OK    HELLO  ->  Upload and compile
```

### Step 6: edit, compare, compile

Edit `HELLO.sqlrpgle`, then see what changed compared with the member on IBM i
(`-` is IBM i, `+` is your file):

```sh
ibmicli diff MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
```

Compile for real by dropping `-n`:

```sh
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
```

If the compiler finds errors, they are printed with their location in your
local file:

```text
MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle:42:9
  RNF7030 severe error (30)  The name or indicator WKTOT is not defined.
```

### Step 7: open it in VS Code

```sh
ibmicli vscode install
code ~/sources
```

Open the **repository root**, so the extension can find `ibmiproj.toml`.
Now in any source file:

- **F6** opens the menu: `compile`, compare with IBM i, run SQL, debug.
- **F12** on a `/copy` opens the copybook.
- Compiler errors appear in the **Problems** panel.

### Your daily routine

| To... | Run (or press F6 in VS Code) |
| --- | --- |
| Get the latest version of a member | `ibmicli pull -lib MYAPP_SRC -file QRPGLESRC -mbr HELLO -f -proj ../ibmiproj.toml` (from `MYAPP/`) |
| See every file that differs from IBM i | `ibmicli status -proj ibmiproj.toml` |
| Compile one file | `ibmicli run compile -src FILE -proj ibmiproj.toml` |
| Upload and compile everything changed since the last commit | `ibmicli deploy-build-changed -proj ibmiproj.toml -n`, then without `-n` |
| Query the database | `ibmicli db -proj ../ibmiproj.toml` (from `MYAPP/`) opens an `sql>` prompt with the app's library list |

## Configuration in detail

### The global file: `config.toml`

`~/.config/ibmicli/config.toml`, readable only by you. `set-conn` and `env set`
write it; you can also edit it. A complete example:

```toml
# Written by set-conn
host = "10.0.0.5"
user = "MYUSER"
password = "..."
ccsid = "1144"                       # optional, server CCSID
remote_charset = "iso-8859-1"        # encoding of the bytes sent over FTP

# Optional settings: always at the top, before any [table]
tasks_key = "f6"                     # VS Code menu shortcut
debug_host = "ibmi.example.com"      # see "debug"
env = "dev"                          # active environment, for db and the shell

# Environments (see below)
[envs.dev]
setup = ["CALL TOOLS/ENVPGM"]
curlib = "MYAPP_OBJ"
libl = ["MYAPP_SRC", "MYAPP_DAT", "TOOLS"]

# Tasks shared by every repository on this machine
[tasks.wrkactjob]
steps = ["WRKACTJOB"]
```

Top-level keys such as `tasks_key` and `debug_host` must come **before** the
first `[table]`: in TOML, a key written after `[envs.dev]` belongs to that
environment. `ibmicli config` shows the effective values (never the password).

`XDG_CONFIG_HOME` moves the configuration directory; `IBMICLI_CONFIG` points
to a specific file, useful for tests.

### Environments

An environment describes one thing: the job's library list. There are two
ways to write one, and you choose per environment.

**With your own setup CL.** Use this if you already have an environment
program. You write `setup`; ibmicli runs it, reads the result and writes
`curlib` and `libl` for you:

```toml
[envs.dev]
setup = ["CALL TOOLS/ENVPGM"]        # you write this
curlib = "MYAPP_OBJ"                 # ibmicli writes these two
libl = ["MYAPP_SRC", "MYAPP_DAT", "TOOLS"]
```

The saved list lets VS Code, debug and `-n` work without connecting. After
changing the environment program on IBM i, run `ibmicli env refresh dev` (or
`env refresh -all`).

**With an explicit list.** Leave out `setup` and write the list yourself;
ibmicli turns it into a `CHGLIBL`:

```toml
[envs.test]
curlib = "MYAPP_OBJ"
libl = ["MYAPP_SRC", "MYAPP_DAT"]
```

**Which environment is used:**

| Command | Environment |
| --- | --- |
| `run` (tasks), `debug`, `deploy-build-changed` | the app's `env` in `ibmiproj.toml`, or `-var env=NAME` |
| `db` | with `-proj`, the environment of the app you are in (or of the `-from` file); otherwise the active one |
| the interactive shell | the active one (`ibmicli env use NAME`) |
| `deploy`, `pull`, `list`, `show`, `diff` | none: they only transfer files |

The active environment is never used as a fallback for compiling: a task for a
file outside any app needs `-var env=NAME`.

The **current library** is the default object library, unless the app sets
`objlib`. An empty current library or QGPL is refused as an object library, to
avoid compiling there by mistake. `/copy` directives without a library search
the current library, then the library list in order, as the compiler does.

| Command | Does |
| --- | --- |
| `ibmicli env set NAME` | create or edit an environment (asks the questions shown in step 2) |
| `ibmicli env ls` | show all environments and their libraries |
| `ibmicli env refresh NAME` / `-all` | re-read the library list from IBM i |
| `ibmicli env use NAME` | choose the active environment for `db` and the shell |
| `ibmicli env` | show the active environment |

### The project file: `ibmiproj.toml`

One file at the repository root, committed to git, with no credentials. It
contains:

```toml
# Apps: each one is a folder directly below the root
[apps.MYAPP]
env = "dev"                # environment from config.toml; "" = none
objlib = "MYAPP_OBJ"       # object library; "" = the environment's current library

[apps.SHARED]              # an app used only for reading (copybooks)
env = ""
objlib = ""

# Folder names that differ from IBM i names (optional)
[libraries]
SOURCES = "MYAPP_SRC"      # folder SOURCES is library MYAPP_SRC
[srcfile]
RPG = "QRPGLESRC"          # folder RPG is source file QRPGLESRC

# Repository tasks (optional), see "Tasks"
[tasks.compile]
steps = ["..."]
```

`ibmicli set-project` adds or changes an app without touching the rest
(handwritten comments are replaced):

```sh
ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml
ibmicli set-project SHARED -env - -objlib - -proj ibmiproj.toml   # '-' means empty
```

Things to know:

- **A folder is an app only if it is declared** in `[apps.NAME]`. Other folders
  (`docs/`...) are ignored, even if their subfolders look like IBM i names.
- **Folder names are IBM i names** by default. `[libraries]` and `[srcfile]`
  map exceptions for all apps; `[apps.NAME.libraries]` and
  `[apps.NAME.srcfile]` do the same for one app and win over the global ones.
- **Allowed keys in `[apps.NAME]`** are `env`, `objlib`, `srclib`,
  `libraries`, `srcfile`. Anything else is an error, so a typo cannot silently
  send a file to the wrong place.
- Folder names with spaces or symbols must be quoted: `[apps."MY APP"]`.

#### Apps with a single source library

If an app uses only one source library, you can drop the library folder level
with `srclib`:

```text
sources/
  ibmiproj.toml
  api/
    src/                   source file QRPGLESRC (mapped below)
      GIRI.sqlrpgle        member API_SRC/QRPGLESRC(GIRI)
```

```toml
[apps.api]
env = "api"
objlib = ""
srclib = "API_SRC"

[apps.api.srcfile]
src = "QRPGLESRC"
```

The folder after the app is the source file, and the library is always
`srclib`. A `/copy` to another library (say `CGIDEV2/QRPGLESRC,X`) then needs
its own app: `ibmicli set-project CGIDEV2 -env - -srclib CGIDEV2 -proj
ibmiproj.toml`. `-srclib -` turns the app back into one with library folders.

### When `-proj` is needed

`-proj` always names the file, not its folder (`-proj ../ibmiproj.toml` when
you are inside an app). VS Code finds it by walking up from the open file to
the workspace root.

| Command | Without `-proj` | With `-proj` |
| --- | --- | --- |
| `deploy` | `-lib` and `-file` required | taken from the file's path |
| `pull` | downloads to `./LIBRARY/SOURCEFILE` | inside an app folder: into the app, with its folder mappings |
| `list` | `-lib` and `-f` required | inside a source-file folder, both can be omitted |
| `diff` | `-lib` and `-file` required | taken from each file's path |
| `run`, `tasks` | global tasks only; values with `-var` | repository tasks too; values from the app |
| `copy`, `symbols` | no `/copy` resolution across apps | copybooks found in all apps |
| `db` | active environment, or `-lib` | inside an app folder, or with `-from`: the app's environment |
| `status`, `deploy-build-changed`, `debug` | not available | required |
| `show`, `env`, `set-conn`, `config`, `vscode`, `shell` | no project needed | |

## Tasks and compiler errors

Tasks are your build and utility commands. They live only in your files:
`[tasks.NAME]` in `config.toml` (every repository) or in `ibmiproj.toml`
(this repository, wins on the same name). ibmicli ships none. Run one with
`ibmicli run NAME`; list them with `ibmicli tasks`; see the full syntax with
`ibmicli tasks --guide`.

```toml
[tasks.compile]
label = "Upload and compile"   # optional, shown in F6
ext = ["sqlrpgle"]             # extensions it applies to; [] = all
env = true                     # set up the environment before the first CL step
steps = [
  "ibmicli deploy -src {file} -lib {srclib} -file {srcfile}",
  "CRTSQLRPGI OBJ({objlib}/{member}) SRCFILE({srclib}/{srcfile}) SRCMBR({member}) OBJTYPE(*PGM) OPTION(*EVENTF) DBGVIEW(*SOURCE)",
]
```

Editing a task needs no reinstall: the CLI and VS Code read it each time.

### Step types

| Step starts with | Runs | Use for |
| --- | --- | --- |
| `ibmicli ...` | the same CLI | deploy, pull |
| `sh: ...` | your local shell | `git`, scripts |
| `job: ...` | CL via RCMD in the FTP job; changes survive | `ADDLIBLE`, `OVRPRTF` that later steps need |
| `task: NAME` | the steps of another task, inlined | reusing tasks |
| anything else | CL in a QShell child job, with messages | compiling |

An unprefixed `echo hello` is sent to IBM i as CL: use `sh:` for local
commands. Steps stop at the first failure.

Why two kinds of CL: a normal step runs in a child job, which inherits the
library list and returns full messages, but whatever it changes (library list,
overrides, QTEMP) is lost when it ends. A `job:` step changes the FTP job
itself, so the change lasts for the following steps, but only its result
comes back, not its messages.

### Placeholders

| Placeholder | Value |
| --- | --- |
| `{member}` | member name, uppercase, no extension |
| `{file}` | local path as given with `-src` |
| `{dir}`, `{name}`, `{ext}` | local folder, file name, extension (lowercase) |
| `{srctype}` | source type from the extension |
| `{srclib}`, `{srcfile}` | source library and source file from the path |
| `{env}` | the app's environment |
| `{objlib}` | the app's object library, otherwise the current library |
| `{curlib}` | the environment's current library |

`-var NAME=VALUE` overrides any of them, and several pairs can follow one
`-var`: `-var srclib=MYAPP_SRC srcfile=QRPGLESRC env=dev`. `env=NAME` also
selects the environment. A task that uses no source placeholder needs no
`-src`. A nested `ibmicli` step receives neither `-proj` nor `-var`, which is
why the deploy step above passes `-lib {srclib} -file {srcfile}`.

### Compiler errors

With `OPTION(*EVENTF)`, the compiler writes its errors to
`OBJLIB/EVFEVENT(MEMBER)`. ibmicli reads them and maps each one back to your
local file, line and column, including inside copybooks and through SQL
precompilation. The terminal and the VS Code Problems panel can open the
location. A copybook that is not local is shown by its IBM i name.

Do not add another `OPTION(...)` inside `COMPILEOPT` of `CRTSQLRPGI`: the
duplicate parameter stops the RPG compiler (`CPD0042`).

### Building what changed

`deploy-build-changed` uploads and compiles the files git sees as changed. It
needs a task for each source file, in `config.toml`:

```toml
[deploy-build-changed]
QRPGLESRC = { pgm = "buildpgm", srv = "buildsrv" }   # NOMAIN -> srv, otherwise pgm
QCLLESRC = "buildcl"
```

These tasks should compile only (no deploy step): the command uploads
everything first, then builds service programs, then programs. It assumes one
module per object and does not follow dependencies: a changed copybook does
not rebuild the programs that include it.

## VS Code

```sh
ibmicli vscode install
```

Then reload VS Code and **open the repository root**. The extension needs no
npm and finds `ibmiproj.toml` by itself.

- **F6** lists the tasks for the current file, compare with IBM i, run SQL,
  debug, and project-wide deploy/build (which asks for confirmation).
- **Ctrl+click / F12** on a `/copy` opens it, offering to download it if
  missing.
- Completion, hover, go to definition, signatures and Outline cover RPG
  declarations, including those in copybooks.
- Compare and SQL results are shown in memory; no files are written.

The shortcut is `tasks_key` in `config.toml` (default `f6`); changing it needs
`ibmicli vscode install`, changing tasks does not. Set `ibmicli.executable` in
VS Code settings if the CLI is not found.

Companion extensions:

| Extension | Installed by | Purpose |
| --- | --- | --- |
| IBM i Languages (`barrettotte.ibmi-languages`) | `vscode install` | syntax coloring for RPGLE, CLLE, DDS, CL |
| IBM i Debug (`IBM.ibmidebug`) | `vscode install` | batch and service-entry-point debugging |
| RPGLE (`halcyontechltd.vscode-rpgle`) | `vscode install -extra` | linter and language tools, with its own Outline |
| Code for IBM i (`halcyontechltd.code-for-ibmi`) | `vscode install -extra` | browse remote libraries and members |

None of the optional ones is required. When the RPGLE extension is installed,
the ibmicli Outline turns itself off to avoid showing everything twice.

## Command reference

`ibmicli --help` and `ibmicli COMMAND --help` list every option. Exit codes:
`0` done, `1` operation failed, `2` usage or configuration error. With
`--json`, stdout contains only JSON and messages go to stderr. Examples below
assume the repository from the walkthrough; `-n` shows the plan without doing
anything.

### deploy

Upload local files to members. Choose one of `-src FILE`, `-srcs FILE1,FILE2`
or `-all` (files in the current folder). The member name is the file name
without extension, uppercase, at most ten characters; the source type is the
extension (`-t TYPE` overrides it). Existing members are overwritten.

```sh
ibmicli deploy -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
ibmicli deploy -src HELLO.sqlrpgle -lib MYAPP_SRC -file QRPGLESRC -n
```

### pull

Download members. Choose `-mbr`, `-mbrs A,B` or `-all` with `-lib`/`-file`, or
`-path LIBRARY/FILE,MEMBER` (the format printed by `list`). `-o FOLDER` sets
the destination; otherwise see [When `-proj` is needed](#when--proj-is-needed).
Existing files are skipped unless `-f`. Extensions come from `[pull_ext]` in
`config.toml` or from the source file name.

```sh
ibmicli pull -path MYAPP_SRC/QRPGLESRC,HELLO -n
ibmicli pull -lib MYAPP_SRC -file QRPGLESRC -all -proj ../ibmiproj.toml -n
```

### list

List the members of a source file, or of every source file with `-f '*ALL'`.
One `LIBRARY/FILE,MEMBER` per line, ready for `pull -path` or `show`.

```sh
ibmicli list -lib MYAPP_SRC -f QRPGLESRC
ibmicli list -lib MYAPP_SRC -f '*ALL'
```

### show

Print members to stdout. `-edit vsc` or `-edit nano` opens a read-only
temporary copy.

```sh
ibmicli show MYAPP_SRC/QRPGLESRC,HELLO
ibmicli show 'MYAPP_SRC/QRPGLESRC(HELLO)' -edit vsc
```

### diff

Compare local files with their members, in memory, as a deploy would send
them (trailing blanks and empty final lines are ignored). `-` is IBM i, `+` is
local. `-U N` sets the context lines. Exit 1 if anything differs.

```sh
ibmicli diff MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
```

### status

Compare every known source file below a folder (default: the current one)
with IBM i. Prints `DIFF` for changed files and `LOCAL` for files missing on
IBM i; `-remote` also lists members missing locally (`IBMI`), `-v` shows equal
files too. Requires `-proj`.

```sh
ibmicli status -proj ibmiproj.toml
ibmicli status MYAPP/MYAPP_SRC/QRPGLESRC -remote -proj ibmiproj.toml
```

### copy

Find the local file of a `/copy`, downloading it if needed. `-from SOURCE`
gives the context; `-all` handles every directive in it (add `-pull` to
download the missing ones); `-no-pull` only reads; `-open` opens the files in
VS Code.

```sh
ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -no-pull -proj ibmiproj.toml
ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -pull -proj ibmiproj.toml
```

### symbols

List free-format RPG declarations, following copybooks. `-local` excludes
copybooks, `-copies` shows only them, `-k KIND` filters by kind, `--stdin`
reads the source from stdin. Never transfers files.

```sh
ibmicli symbols MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
```

### run and tasks

`run NAME` executes a task (see [Tasks](#tasks-and-compiler-errors)); `-v`
prints the full compiler listing. `tasks` lists them, `tasks NAME` shows the
steps, `tasks --guide` explains the syntax.

```sh
ibmicli tasks -proj ibmiproj.toml
ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n
ibmicli run compile -src HELLO.sqlrpgle -var srclib=MYAPP_SRC srcfile=QRPGLESRC env=dev -n
```

### deploy-build-changed

Upload and compile the files changed since the last commit (plus untracked
files) below the current folder. `--since REF` counts from the point where
the branch left `REF`. `--json` prints the plan without running it. Requires
`-proj` and the `[deploy-build-changed]` table described above.

```sh
ibmicli deploy-build-changed -proj ibmiproj.toml -n
ibmicli deploy-build-changed --since main -proj ibmiproj.toml -n
```

Until you commit, running it again uploads the same files again.

### debug

Prepare the settings to debug a program with IBM's extension; VS Code runs it
from F6. `-sep` prepares a service entry point instead of a batch job.
Requires `-proj`. Two keys in `config.toml` (asked by `set-conn`):

- `debug_host`: the IBM i **host name** covered by its TLS certificate, not
  the IP address.
- `debug_batch`: the command that submits the program, for example
  `SBMJOB CMD(CALL PGM({objlib}/{member})) JOBQ(QSYS/QSYSNOMAX) CPYENVVAR(*YES)`.

Compile with `DBGVIEW(*SOURCE)` or `*ALL`.

```sh
ibmicli debug MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml
```

### db

Run SQL through ODBC. Without SQL text it opens an `sql>` prompt. The library
list is `-lib LIB1,LIB2` if given; with `-proj`, that of the app you are in
(or of the `-from` file); otherwise the active environment's.

```sh
ibmicli db "select * from customers fetch first 5 rows only"
cd MYAPP && ibmicli db -proj ../ibmiproj.toml
ibmicli db -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -line 120 --var CUST="'C001'" -proj ibmiproj.toml
ibmicli db --check
```

- **Read-only by default.** `--write` allows changes, committed immediately.
  `--write --tx` uses a transaction: only typing `commit` at the prompt
  confirms; exiting, closing the terminal or losing the connection rolls back.
  Tables need journaling for `--tx` (`SQL7008` otherwise).
- **Output**: a table by default, `--json` (one line per statement) or
  `--csv`. `--out FILE` writes the result to a file. There is no row limit:
  add `fetch first N rows only`.
- **From a source**: `-from FILE -line N` takes the `exec sql` at that line,
  drops `INTO`, and replaces host variables with the `--var NAME=VALUE` values
  (written as SQL, quotes included).
- **At the prompt**: Enter adds a line; a final `;`, an empty line or
  Esc+Enter runs. Up recalls whole statements; TAB completes keywords and
  table names.

### set-conn, set-project, env, config

```sh
ibmicli set-conn                 # connection settings; --no-test skips the checks
ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml
ibmicli env ls                   # see "Environments"
ibmicli config                   # effective settings, without the password
```

`set-conn` keeps your environments, tasks and other settings when it rewrites
the file.

### vscode

```sh
ibmicli vscode install           # install or update the extension and its companions
ibmicli vscode install -extra    # also RPGLE and Code for IBM i
ibmicli vscode install -only     # only the ibmicli extension
ibmicli vscode status
ibmicli vscode uninstall         # removes only the ibmicli extension
```

### shell

`ibmicli` with no arguments opens an interactive shell: the same commands
without the `ibmicli` prefix, over a single FTP connection kept open until
you exit. The prompt shows the active environment. Unknown commands, pipes
and redirections go to your local shell.

## Scope and limits

FTP handles sources and CL; only `db` uses ODBC. ibmicli does not replace a
5250 session or the IBM debugger, and its RPG symbol reader is not a compiler.

ibmicli is young:

- It has been used on one system (IBM i 7.5) from macOS. Windows and Linux are
  untested, and there is no automated test suite yet.
- FTP is not encrypted (no FTPS), and the password is stored in plain text in
  `config.toml`, readable only by you.
- Uploads go through `remote_charset` (ISO-8859-1 by default). A character it
  cannot represent, such as `€`, becomes `?` and is reported.
- The configuration format is recent and may still change.

ibmicli does not write logs or diff files. Build output goes to the terminal;
comparisons stay in memory. The only files it writes are the sources you
download, its own configuration, the `sql>` history, and `db --out FILE`.

## Common problems

| Message or symptom | What to do |
| --- | --- |
| `configuration not found` | Run `ibmicli set-conn` |
| `missing library and source file` | Pass `-proj FILE` for a file inside an app, or `-lib`/`-file` |
| `task ... is missing {srclib}, ...` | Pass `-proj FILE`, or the values with `-var` |
| Pulled files end up outside the app | Run `pull` from inside the app folder |
| `unknown keys` in an app | Use only `env`, `objlib`, `srclib`, `libraries`, `srcfile` in `[apps.NAME]` |
| `environment ... does not exist` | Create it with `ibmicli env set NAME`, or fix the app's `env` |
| Unknown object library | Set `objlib` for the app, or `ibmicli env refresh NAME` |
| Copybook missing locally | `ibmicli copy -from FILE -all -pull -proj ibmiproj.toml` |
| Works in the terminal but not in VS Code | Open the repository root in VS Code |
| `driver ... is not installed` | Run `ibmicli db --check` and follow its instructions |
| `db is read-only` | Add `--write` (and `--tx` for a transaction) |
| `SQL7008` with `--tx` | Journal the table, or use `--write` without `--tx` |
| Compiler errors without file and line | Add `OPTION(*EVENTF)` to the compile task |
| `CPD0042` before RPG compilation | Remove the extra `OPTION` from `COMPILEOPT` |
| Service entry point debug fails with a TLS error | Set `debug_host` to the host name, not the IP |
| F6 does something else | Resolve the conflict reported by `vscode install`, or change `tasks_key` |
| Accented characters or `£` arrive wrong | Check `ccsid` and `remote_charset` in `config.toml` |

## Development

```sh
uv sync                        # development environment in .venv
uv tool install --editable .   # the installed ibmicli uses this checkout
```

Python changes are active immediately. Run `./deploy-local` after changing
`pyproject.toml`, and `ibmicli vscode install` after changing the extension.

## License

MIT. See [LICENSE](LICENSE).
