"""List configured tasks or describe one; supply the same filtered list to VS Code."""

from __future__ import annotations

import json
from pathlib import Path

from .. import output, project as prj, tasks as tasks_mod
from ..config import Config
from ..tasks import Task


# The task guide lives with the CLI so it stays current
# after updates. Keeping the text together makes it easier
# to maintain without duplicated explanations.
GUIDE = """[tasks."NAME"]
label = "Menu label"              # optional
ext   = ["sqlrpgle"]              # [] accepts every extension
env   = true                      # set up before the first CL
steps = [ ... ]                   # executed in order

Steps stop at the first failure. Their prefixes select five kinds:

  ibmicli deploy -src {file}    a command in this CLI
  sh: ./script.sh {file}        a local shell command
  task: other-task              another task's steps, inserted here
  job: ADDLIBLE MYLIB           CL setup in the persistent FTP job
  CRTSQLRPGI OBJ(...)           CL in a separate QShell child job

An unprefixed step is CL. 'echo hello' goes to IBM i and fails with CPD0030;
use 'sh: echo hello' to run it locally.

Normal CL inherits the FTP job library list, but uses separate QTEMP and
does not inherit file overrides. 'job:' can preserve library-list changes
for later steps; it returns status only, without a listing or messages.
An ADDLIBLE in a normal CL step disappears with that child job.

Only tasks that use source placeholders require -src. Environment values
or env = true with CL require an environment. Standalone tasks can run as
'ibmicli run NAME' without a source or environment.

Values come from -proj FILE or -var NAME=VALUE. The project supplies the
source app environment, objlib (or environment current library) and the
LIBRARY/SOURCEFILE folders. Outside declared apps nothing is inferred.

  ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n

Explicit -var values always win. More pairs can follow one -var, or the
option can be repeated. env=NAME selects setup and current library as well.

  ibmicli run compile -src HELLO.sqlrpgle -var srclib=MYAPP_SRC srcfile=QRPGLESRC objlib=MYAPP_OBJ env=dev -n

An ibmicli step is a separate command: it does not inherit -proj or -var.
Write deploy as 'ibmicli deploy -src {file} -lib {srclib} -file {srcfile}'.
Included tasks are flattened before execution. They use the outer task's
source, environment and extension filter; dry-run shows the actual sequence.

Global tasks live in config.toml. Repository [tasks] in ibmiproj.toml apply
with -proj and override global tasks with the same name.
"""

# A complete compilation example to adapt.
EXAMPLE = """Example to copy and adapt:

  [tasks."compile"]
  label = "Compile SQLRPGLE module"
  ext   = ["sqlrpgle"]
  env   = true
  steps = [
    "ibmicli deploy -src {file} -lib {srclib} -file {srcfile}",
    "CRTSQLRPGI OBJ({objlib}/{member}) SRCFILE({srclib}/{srcfile}) SRCMBR({member}) OBJTYPE(*MODULE) OPTION(*EVENTF)",
  ]

{srclib} and {srcfile} come from the project source folders, or explicit
-var values. OPTION(*EVENTF) writes compiler diagnostics to the event file;
ibmicli maps them to file:line:column in the terminal and VS Code Problems.
"""


def run(
    cfg: Config,
    name: str | None = None,
    ext: str | None = None,
    as_json: bool = False,
    guide: bool = False,
    project: prj.Project | None = None,
) -> int:
    """List global/project tasks, with project definitions taking precedence."""
    if guide:
        return _guide(cfg)

    available, where = prj.tasks_for(cfg, project)
    if name:
        found = [cfg.require_task(name, available)]
    else:
        found = [task for task in available.values() if ext is None or task.accepts(ext)]

    if as_json:
        known = set(available)
        print(
            json.dumps(
                {"tasks": [_as_dict(task, known) for task in found]}, ensure_ascii=False
            )
        )
        return 0

    if not found:
        if ext:
            output.error(f"no tasks for sources .{ext.lstrip('.').lower()}")
        else:
            output.error(
                f"no tasks defined in {cfg.path}\n"
                f"       define tasks there or in the explicit project;\n"
                f"       step types and placeholders are described by\n"
                f"       ibmicli tasks --guide"
            )
        return 1

    if name:
        _show(found[0], available, where)
    else:
        _table(found, available, where, cfg.path)
    return 0


def _guide(cfg: Config) -> int:
    """Current task syntax and placeholders, kept out of generated user configuration."""
    print(f"Define global tasks in {cfg.path}, repository tasks in {prj.PROJECT_FILE}.")
    print()
    print(GUIDE)
    print("Available step placeholders:")
    width = max(len(name) for name, _ in tasks_mod.PLACEHOLDERS)
    for name, what in tasks_mod.PLACEHOLDERS:
        print(f"  {{{name}}}{' ' * (width - len(name))}  {what}")
    print()
    print(EXAMPLE)
    return 0


def _as_dict(task: Task, known: set[str]) -> dict[str, object]:
    return {
        "name": task.name,
        "label": task.title,
        "ext": list(task.ext),
        "steps": list(task.steps),
        "problems": tasks_mod.problems(task, known=known),
    }


def _table(
    found: list[Task], available: dict[str, Task], where: dict[str, Path], global_path: Path
) -> None:
    known = set(available)
    width = max(len(task.name) for task in found)
    for task in found:
        extensions = ", ".join(f".{e}" for e in task.ext) or "any file"
        local = "  (project)" if where[task.name] != global_path else ""
        print(f"{task.name:<{width}}  {extensions:<22}  {task.title}{local}")
        for trouble in tasks_mod.problems(task, known=known):
            print(f"{'':<{width}}  WARNING: {trouble}")
    print()
    print("Run with 'ibmicli run NAME'; inspect steps with 'ibmicli tasks NAME';")
    print("in VS Code, open the menu with F6.")


def _show(task: Task, available: dict[str, Task], where: dict[str, Path]) -> None:
    print(f"{task.name}: {task.title}")
    print(f"  extensions: {', '.join(f'.{e}' for e in task.ext) or 'any file'}")
    print(f"  environment:   {'yes' if task.env else 'no'}")
    print("  steps:")
    for step in task.steps:
        print(f"    {step}")
    for trouble in tasks_mod.problems(task, known=set(available)):
        print(f"  WARNING: {trouble}")
    print()
    print(f"run with: ibmicli run {task.name} -src FILE")
    print(f"edit in: {where[task.name]}")
