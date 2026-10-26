"""User-defined tasks and their step syntax.

Tasks live in global config.toml or the explicit project's ibmiproj.toml.
This module has no configuration or IBM i dependencies, so config.py can
import it without a cycle. commands/run.py supplies placeholder values.
Unprefixed steps are CL commands, including misspelled local commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

# A CLI-prefixed step reenters the same command application.
CLI_PREFIX = "ibmicli"

# RCMD steps run in the FTP job, preserving its library list
# for subsequent steps. ADDLIBLE, CHGLIBL and environment CALLs
# belong here. Normal CL runs in a child QShell job;
# its state changes disappear when it exits.
JOB_PREFIX = "job:"

# Local shell commands need an explicit prefix;
# without it, a step would be sent to IBM i as CL.
SH_PREFIX = "sh:"

# Task inclusion replaces a step with another task's steps.
# This keeps repeated deploy, setup or compilation sequences
# in one definition.
# Flattening happens before execution in commands/run.py.
# Included steps use the outer task's source and environment,
# and dry-run displays the flattened sequence.
TASK_PREFIX = "task:"

# TASK disappears during flattening; only executable kinds
# reach the runner.
CLI, SH, JOB, CL, TASK = "cli", "sh", "job", "cl", "task"

# Placeholder descriptions in display order for
# 'ibmicli tasks --guide'.
PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("member", "uppercase member name: HELLO"),
    ("file", "local file path as supplied on the command line"),
    ("dir", "local file folder"),
    ("name", "local filename with extension: HELLO.sqlrpgle"),
    ("ext", "local file extension: sqlrpgle"),
    ("srctype", "member SRCTYPE: SQLRPGLE"),
    ("srcfile", "source file: project folder in LIBRARY/SOURCEFILE/file"),
    ("env", "environment name: 'env' of the source app"),
    ("objlib", "object library: app 'objlib', otherwise the current library"),
    ("srclib", "source library: library folder in the app"),
    ("curlib", "environment current library"),
)

KNOWN = {name for name, _ in PLACEHOLDERS}

# Source-dependent placeholders require -src.
# Standalone shell or CL tasks need no invented member.
SOURCE = frozenset(
    {"member", "file", "dir", "name", "ext", "srctype", "srcfile"}
)

# Environment or project-dependent values.
# Tasks without these values or environment setup
# can run without an environment.
FROM_ENV = frozenset({"env", "objlib", "srclib", "curlib"})


@dataclass(frozen=True)
class Task:
    """A named sequence of steps."""

    name: str
    label: str = ""
    ext: tuple[str, ...] = ()
    env: bool = True  # Set up the environment before the first CL.
    steps: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        """Display label, falling back to the task name."""
        return self.label or self.name

    def accepts(self, ext: str) -> bool:
        """Whether the task accepts an extension; no filter accepts all."""
        return not self.ext or ext.strip().lstrip(".").lower() in self.ext

    @property
    def needs_ibmi(self) -> bool:
        """Whether at least one step is a CL or job command."""
        return any(kind_of(step) in (CL, JOB) for step in self.steps)


# Quote placeholder values in steps that are split into words,
# so paths containing spaces remain one argument.
QUOTED = (CLI, SH)


def kind_of(step: str) -> str:
    """Classify a step: CLI, SH, JOB, CL or TASK."""
    text = step.strip()
    if text.lower().startswith(JOB_PREFIX):
        return JOB
    if text.lower().startswith(SH_PREFIX):
        return SH
    if text.lower().startswith(TASK_PREFIX):
        return TASK
    first = text.split(maxsplit=1)[0].lower() if text else ""
    return CLI if first == CLI_PREFIX else CL


def body(step: str) -> str:
    """Step without its kind prefix."""
    text = step.strip()
    kind = kind_of(text)
    if kind == JOB:
        return text[len(JOB_PREFIX):].strip()
    if kind == SH:
        return text[len(SH_PREFIX):].strip()
    if kind == TASK:
        return text[len(TASK_PREFIX):].strip()
    if kind == CLI:
        # 'ibmicli deploy -src X' -> 'deploy -src X'; a bare prefix does nothing.
        _, _, rest = text.partition(" ")
        return rest.strip()
    return text


def used(step: str) -> list[str]:
    """Placeholders in first-use order."""
    found = []
    for _, name, _, _ in Formatter().parse(step):
        if name and name not in found:
            found.append(name)
    return found


def problems(task: Task, known: set[str] | None = None) -> list[str]:
    """Definition problems that can be found without source or environment values.

    Invalid tasks are listed with their problems and refused by run, without
    breaking unrelated CLI commands. known also validates included task names.
    """
    found = []
    if not task.steps:
        found.append("no steps: missing 'steps'")
    for step in task.steps:
        kind = kind_of(step)
        name = body(step)
        if not name:
            found.append(f"empty step: {step.strip() or '(empty line)'}")
            continue
        if kind == TASK:
            if name == task.name:
                found.append(f"'task: {name}' includes itself")
            elif known is not None and name not in known:
                found.append(f"'task: {name}' does not exist")
            continue
        for placeholder in used(step):
            if placeholder not in KNOWN:
                found.append(f"unknown placeholder {{{placeholder}}} in: {step}")
    return found


def _texts(value: object) -> tuple[str, ...]:
    """A string or sequence -> tuple of nonempty strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def read(raw: object) -> dict[str, Task]:
    """Read only tasks present in configuration.

    Missing and empty task sections both mean no tasks. There is no fallback.
    """
    if not isinstance(raw, dict):
        return {}

    tasks: dict[str, Task] = {}
    for name, values in raw.items():
        if not isinstance(values, dict):
            continue
        key = str(name).strip()
        if not key:
            continue
        tasks[key] = Task(
            name=key,
            label=str(values.get("label", "") or "").strip(),
            ext=tuple(
                item.lstrip(".").lower() for item in _texts(values.get("ext"))
            ),
            env=bool(values.get("env", True)),
            steps=_texts(values.get("steps")),
        )
    return tasks
