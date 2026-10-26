"""Display effective global configuration, including defaults.

Only report whether a password exists; never display its value. Tasks have
no defaults and exist only in user configuration.
"""

from __future__ import annotations

from .. import tasks as tasks_mod
from ..config import PULL_EXT_DEFAULTS, Config
from .debug import DEBUG_PORT, SEP_DAEMON_PORT


def run(cfg: Config, short: bool = False) -> int:
    _connection(cfg)
    _transfer(cfg)
    _debug(cfg)
    if not short:
        _srctype(cfg)
        _pull_ext(cfg)
    _environments(cfg, short)
    _task(cfg, short)
    if short:
        print()
        print("Run 'ibmicli config' without -s for full details.")
    return 0


def _heading(text: str, extra: str = "") -> None:
    line = f"{text}{'   ' + extra if extra else ''}"
    print()
    print(line)
    print("-" * len(line))


def _connection(cfg: Config) -> None:
    print(f"file:      {cfg.path}")
    print(f"host:      {cfg.host}")
    print(f"user:    {cfg.user}")
    # Never display the password, only whether it is set.
    print(f"password:  {'set' if cfg.password else 'MISSING'}")


def _transfer(cfg: Config) -> None:
    _heading("TRANSFER")
    print(f"ccsid:       {cfg.ccsid or '-  (from the IBM i user profile)'}")
    print(f"set_srctype: {'yes' if cfg.set_srctype else 'no'}")
    print(f"encoding:    {cfg.local_charset} (local)  <->  {cfg.remote_charset} (server)")
    print("             the FTP server converts ISO-8859-1 <-> EBCDIC")


def _debug(cfg: Config) -> None:
    _heading("VS CODE DEBUG")
    if cfg.debug_host:
        print(f"debug_host:  {cfg.debug_host}  (Debug Service on port {DEBUG_PORT}, "
              f"SEP {SEP_DAEMON_PORT})")
    else:
        print("debug_host:  -  (missing: 'ibmicli debug --help')")
    print(f"debug_batch: {cfg.debug_batch or '-'}")


def _srctype(cfg: Config) -> None:
    _heading("MEMBER SRCTYPE BY EXTENSION")
    print("defaults to the uppercase extension (sqlrpgle -> SQLRPGLE);")
    print("overrides from [srctype] follow.")
    if not cfg.srctype:
        print("  no overrides")
        return
    width = max(len(ext) for ext in cfg.srctype)
    for ext, srctype in sorted(cfg.srctype.items()):
        print(f"  {ext:<{width}}  ->  {srctype}")


def _pull_ext(cfg: Config) -> None:
    """Effective download extensions, marking values supplied in [pull_ext]."""
    _heading("DOWNLOAD EXTENSION BY SOURCE FILE")
    effective = dict(PULL_EXT_DEFAULTS)
    effective.update(cfg.pull_ext)
    width = max(len(name) for name in effective)
    for srcfile, ext in sorted(effective.items()):
        mark = " *" if srcfile in cfg.pull_ext else ""
        print(f"  {srcfile:<{width}}  ->  .{ext}{mark}")
    print(f"  {'others':<{width}}  ->  .txt")
    if cfg.pull_ext:
        print("* configured in [pull_ext]; remaining entries are defaults.")


def _environments(cfg: Config, short: bool) -> None:
    active = cfg.env()
    _heading(
        f"ENVIRONMENTS ({len(cfg.envs)})",
        f"active: {active.name if active else 'none'}",
    )
    if not cfg.envs:
        print("none; create one with: ibmicli env set NAME")
        return
    if active is None:
        print("none active; select one with: ibmicli env use NAME")
    if short:
        print(", ".join(sorted(cfg.envs)))
        return

    for name in sorted(cfg.envs):
        env = cfg.envs[name]
        print()
        print(f"{name}{'   (active)' if active and name == active.name else ''}")
        for command in env.commands:
            print(f"  setup   {command}{'' if env.setup else '   (from curlib and libl)'}")
        print(f"  curlib  {env.curlib or '-'}")
        print(f"  libl    {' '.join(env.libl) or '-'}")


def _task(cfg: Config, short: bool) -> None:
    _heading(f"TASK ({len(cfg.tasks)})")
    if not cfg.tasks:
        print("none. Define tasks in the configuration file above.")
        print("Step types, placeholders and an example are available with")
        print("ibmicli tasks --guide.")
        return
    if short:
        print(", ".join(cfg.tasks))
        return

    for task in cfg.tasks.values():
        print()
        print(f"{task.name}   {task.title}")
        extensions = ", ".join(f".{e}" for e in task.ext) or "any file"
        print(f"  extensions: {extensions}   environment: {'yes' if task.env else 'no'}")
        for index, step in enumerate(task.steps, start=1):
            print(f"  {index}. {_step(step)}")
        for trouble in tasks_mod.problems(task):
            print(f"  WARNING: {trouble}")


def _step(step: str) -> str:
    """Prefix a task step with its execution destination."""
    kind = tasks_mod.kind_of(step)
    if kind == tasks_mod.CLI:
        return f"[cli] {step}"
    if kind == tasks_mod.SH:
        return f"[sh]  {tasks_mod.body(step)}"
    if kind == tasks_mod.TASK:
        return f"[task] steps from '{tasks_mod.body(step)}'"
    if kind == tasks_mod.JOB:
        return f"[job] {tasks_mod.body(step)}"
    return f"[cl]  {step}"
