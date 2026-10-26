"""Global CLI configuration, stored in the user's home directory.

    ~/.config/ibmicli/config.toml (or XDG_CONFIG_HOME / IBMICLI_CONFIG)

Credentials, environments and global tasks live here. Project files contain
no credentials. Configuration files use mode 600.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import tasks as tasks_mod
from .tasks import Task

# Download extensions by source file, overridden by
# [pull_ext] in the global configuration.
PULL_EXT_DEFAULTS = {
    "QRPGLESRC": "sqlrpgle",
    "QRPGAPI": "rpgle",
    "QPROTSRC": "rpgle",
    "QCPYSRC": "rpgle",
    "QCLLESRC": "clle",
    "QCMDSRC": "cmd",
    "QCSRC": "c",
    "QDDLSRC": "sql",
    "QSQLSRC": "sql",
    "QBNDSRC": "bnd",
}

# The task menu shortcut is stored only in tasks_key.
# The extension package reads it when built by
# 'ibmicli vscode install'.
TASKS_KEY = "f6"


class ConfigError(Exception):
    """Missing or invalid configuration."""


def config_path() -> Path:
    """Configuration path, overridable for tests."""
    override = os.environ.get("IBMICLI_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "ibmicli" / "config.toml"


@dataclass
class Env:
    """A job library-list environment.

    With setup commands, curlib and libl are read from IBM i by env set or
    env refresh. Without setup, the supplied values generate CHGLIBL.
    Cached library lists also support offline copy resolution and debug plans.
    """

    name: str
    setup: tuple[str, ...] = ()
    curlib: str = ""
    libl: tuple[str, ...] = ()  # The user portion, in job order.

    @property
    def commands(self) -> tuple[str, ...]:
        """Environment CL commands for RCMD, which preserves job state."""
        if self.setup:
            return self.setup
        if self.libl:
            cur = f" CURLIB({self.curlib})" if self.curlib else ""
            return (f"CHGLIBL LIBL({' '.join(self.libl)}){cur}",)
        if self.curlib:
            return (f"CHGCURLIB CURLIB({self.curlib})",)
        return ()

    @property
    def search(self) -> tuple[str, ...]:
        """Library search order: current library, then the user portion."""
        found: list[str] = []
        for name in (self.curlib, *self.libl):
            if name and name not in found:
                found.append(name)
        return tuple(found)


@dataclass(frozen=True)
class Build:
    """Build task selection for one source file.

    Stored in [deploy-build-changed]. With one module per object, NOMAIN
    selects srv; modules with an entry point select pgm. A string sets both.
    """

    pgm: str = ""
    srv: str = ""


@dataclass
class Config:
    host: str
    user: str
    password: str
    path: Path
    ccsid: str | None = None
    set_srctype: bool = True
    # The FTP server converts EBCDIC <-> ISO-8859-1; local files use UTF-8.
    local_charset: str = "utf-8"
    remote_charset: str = "iso-8859-1"
    tasks_key: str = TASKS_KEY
    # Debug settings have no defaults: the system hostname and batch command
    # must be supplied by the user.
    debug_host: str = ""
    debug_batch: str = ""
    srctype: dict[str, str] = field(default_factory=dict)
    pull_ext: dict[str, str] = field(default_factory=dict)
    envs: dict[str, Env] = field(default_factory=dict)
    # Tasks retain configuration order, which is also the order of the
    # VS Code task menu.
    tasks: dict[str, Task] = field(default_factory=dict)
    # Source file -> build task. Source files without a configured task
    # are uploaded without compilation.
    build: dict[str, Build] = field(default_factory=dict)
    # Invalid build entries are reported only by deploy-build-changed,
    # so they cannot break unrelated commands.
    build_problems: list[str] = field(default_factory=list)
    active: str = ""  # Active environment selected by 'env use'.

    def env(self) -> Env | None:
        """Active environment, or None if none is selected."""
        return self.envs.get(self.active) if self.active else None

    def require_env(self) -> Env:
        """Active environment; raise a configuration error if missing."""
        current = self.env()
        if current is None:
            if not self.envs:
                raise ConfigError(
                    "no environments defined\n"
                    "       create one with: ibmicli env set NAME"
                )
            raise ConfigError(
                "no active environment\n"
                f"       select one with: ibmicli env use {sorted(self.envs)[0]}\n"
                f"       available: {', '.join(sorted(self.envs))}"
            )
        return current

    def named_env(self, name: str) -> Env:
        """Named environment, with available names in the error if missing."""
        found = self.envs.get(name)
        if found is None:
            raise ConfigError(
                f"environment '{name}' does not exist\n"
                f"       create it with: ibmicli env set {name}\n"
                f"       available: {', '.join(sorted(self.envs)) or 'none'}"
            )
        return found

    def require_task(self, name: str, tasks: dict[str, Task] | None = None) -> Task:
        """Requested task, optionally from a merged global/project task list."""
        tasks = self.tasks if tasks is None else tasks
        found = tasks.get(name)
        if found is None:
            if not tasks:
                raise ConfigError(
                    f"no tasks defined: '{name}' does not exist\n"
                    f"       define tasks in {self.path}; see\n"
                    f"       ibmicli tasks --guide for steps and placeholders"
                )
            raise ConfigError(
                f"task '{name}' does not exist\n"
                f"       available: {', '.join(tasks)}\n"
                f"       list them with 'ibmicli tasks'; define them in {self.path}"
            )
        return found

    def srctype_for(self, ext: str) -> str:
        """File extension -> member SRCTYPE; defaults to the uppercase extension."""
        return self.srctype.get(ext.lower(), ext.upper())

    @property
    def known_srcfiles(self) -> set[str]:
        """Source files from built-in mappings, [pull_ext] and [deploy-build-changed].

        Other project folders, such as MD, JSON or templates, are not uploaded.
        """
        return set(PULL_EXT_DEFAULTS) | set(self.pull_ext) | set(self.build)

    def ext_for_srcfile(self, srcfile: str) -> str:
        srcf = srcfile.upper()
        return self.pull_ext.get(srcf) or PULL_EXT_DEFAULTS.get(srcf, "txt")


def exists() -> bool:
    return config_path().is_file()


def load() -> Config:
    path = config_path()
    if not path.is_file():
        raise ConfigError(
            f"configuration not found at {path}\n"
            f"       create it with: ibmicli set-conn"
        )
    raw = read_raw(path)
    if "profiles" in raw:
        raise ConfigError(
            f"{path}: unsupported configuration key 'profiles'\n"
            f"       define environments with: ibmicli env set NAME"
        )
    return from_raw(raw, path)


def read_raw(path: Path) -> dict:
    """Read TOML without interpreting its values."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def from_raw(raw: dict, path: Path) -> Config:
    """Configuration from a parsed dictionary."""
    missing = [k for k in ("host", "user", "password") if not str(raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"{path}: missing {', '.join(missing)}; run: ibmicli set-conn")

    build, build_problems = _read_build(raw.get("deploy-build-changed"))

    return Config(
        host=str(raw["host"]).strip(),
        user=str(raw["user"]).strip(),
        password=str(raw["password"]),
        path=path,
        ccsid=str(raw["ccsid"]) if raw.get("ccsid") else None,
        tasks_key=str(raw.get("tasks_key") or TASKS_KEY).strip().lower(),
        debug_host=str(raw.get("debug_host") or "").strip(),
        debug_batch=str(raw.get("debug_batch") or "").strip(),
        set_srctype=bool(raw.get("set_srctype", True)),
        local_charset=str(raw.get("local_charset", "utf-8")),
        remote_charset=str(raw.get("remote_charset", "iso-8859-1")),
        srctype={str(k).lower(): str(v).upper() for k, v in (raw.get("srctype") or {}).items()},
        pull_ext={str(k).upper(): str(v) for k, v in (raw.get("pull_ext") or {}).items()},
        envs=_read_envs(raw.get("envs") or {}),
        # Only user-defined tasks exist; there is no fallback task list.
        tasks=tasks_mod.read(raw.get("tasks")),
        build=build,
        build_problems=build_problems,
        active=str(raw.get("env") or "").strip(),
    )


def _read_build(raw: object) -> tuple[dict[str, Build], list[str]]:
    """Read [deploy-build-changed].

    SOURCEFILE = "task" or SOURCEFILE = { pgm = "...", srv = "..." }.
    Invalid entries become problems rather than blocking the entire CLI.
    """
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["[deploy-build-changed] must be a table"]
    build: dict[str, Build] = {}
    problems: list[str] = []
    for srcfile, value in raw.items():
        key = str(srcfile).strip().upper()
        if isinstance(value, str) and value.strip():
            build[key] = Build(pgm=value.strip(), srv=value.strip())
        elif isinstance(value, dict) and value and set(value) <= {"pgm", "srv"}:
            build[key] = Build(
                pgm=str(value.get("pgm") or "").strip(),
                srv=str(value.get("srv") or "").strip(),
            )
        else:
            problems.append(
                f"[deploy-build-changed] {key}: supply a task (\"crtbndcl\") "
                f"or {{ pgm = \"...\", srv = \"...\" }}"
            )
    return build, problems


def _names(value: object) -> tuple[str, ...]:
    """Library names from a TOML array or a space-separated string."""
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip().upper() for item in value if str(item).strip())


def _read_envs(raw: dict) -> dict[str, Env]:
    envs: dict[str, Env] = {}
    for name, values in raw.items():
        if not isinstance(values, dict):
            continue
        setup = values.get("setup") or ()
        if isinstance(setup, str):
            setup = [setup]
        envs[str(name)] = Env(
            name=str(name),
            setup=tuple(str(cmd).strip() for cmd in setup if str(cmd).strip()),
            curlib=str(values.get("curlib") or "").strip().upper(),
            libl=_names(values.get("libl")),
        )
    return envs


def _toml_str(value: str) -> str:
    """Escape TOML strings, including arbitrary password characters."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for char, code in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        escaped = escaped.replace(char, code)
    return f'"{escaped}"'


def _build_lines(cfg: Config) -> list[str]:
    """Build mappings with a reference to command help."""
    lines = [
        "",
        "# Build tasks by source file for 'deploy-build-changed'.",
        "# Usage: ibmicli deploy-build-changed --help",
    ]
    if not cfg.build:
        return lines + [
            "# [deploy-build-changed]",
            '# QRPGLESRC = { pgm = "buildpgm", srv = "buildsrv" }',
        ]
    lines.append("[deploy-build-changed]")
    for srcfile, build in sorted(cfg.build.items()):
        if build.pgm == build.srv:
            lines.append(f"{srcfile} = {_toml_str(build.pgm)}")
        else:
            parts = [f"{key} = {_toml_str(value)}"
                     for key, value in (("pgm", build.pgm), ("srv", build.srv)) if value]
            lines.append(f"{srcfile} = {{ {', '.join(parts)} }}")
    return lines


def task_lines(tasks: dict[str, Task]) -> list[str]:
    """Task tables, also used by set-project."""
    lines: list[str] = []
    for task in tasks.values():
        lines.append("")
        lines.append(f"[tasks.{_toml_str(task.name)}]")
        lines.append(f"label = {_toml_str(task.label)}")
        lines.append(f"ext = [{', '.join(_toml_str(e) for e in task.ext)}]")
        lines.append(f"env = {'true' if task.env else 'false'}")
        lines.append("steps = [")
        lines += [f"  {_toml_str(step)}," for step in task.steps]
        lines.append("]")
    return lines


def toml_str(value: str) -> str:
    return _toml_str(value)


def _task_lines(cfg: Config) -> list[str]:
    """Task tables with a reference to the current guide.

    Documentation lives in tasks --guide: copying it into user configuration
    would leave stale instructions after a CLI update.
    """
    lines = [
        "",
        "# Tasks appear in the VS Code menu opened with",
        f"# {cfg.tasks_key.upper()}; run them with 'ibmicli run NAME -src FILE'.",
        "# Only configured tasks exist. Removing one removes it from",
        "# the CLI task list and menu.",
        "#",
        "# Step types and available placeholders:",
        "#   ibmicli tasks --guide",
    ]
    return lines + task_lines(cfg.tasks)


def save(cfg: Config) -> Path:
    """Write configuration, creating its parent directory and setting mode 600."""
    path = cfg.path
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ibmicli configuration. Contains a password: keep mode 600.",
        "# Regenerate with: ibmicli set-conn --force",
        "",
        f"host = {_toml_str(cfg.host)}",
        f"user = {_toml_str(cfg.user)}",
        f"password = {_toml_str(cfg.password)}",
        "",
        (f"ccsid = {_toml_str(cfg.ccsid)}" if cfg.ccsid else '# ccsid = "1144"'),
        f"set_srctype = {'true' if cfg.set_srctype else 'false'}",
        f"local_charset = {_toml_str(cfg.local_charset)}",
        f"remote_charset = {_toml_str(cfg.remote_charset)}",
        "",
        "# VS Code task menu shortcut (tasks are at the end of this file).",
        "# Any VS Code key combination is accepted: f6, cmd+f9, ...",
        "# After changing it, rebuild the extension with 'ibmicli vscode install'.",
        f"tasks_key = {_toml_str(cfg.tasks_key)}",
        "",
        "# VS Code debug: Debug Service hostname (not its IP address)",
        "# and program submission command. Usage:",
        "#   ibmicli debug --help",
        (f"debug_host = {_toml_str(cfg.debug_host)}" if cfg.debug_host else '# debug_host = ""'),
        (f"debug_batch = {_toml_str(cfg.debug_batch)}" if cfg.debug_batch else '# debug_batch = ""'),
        "",
        "# Active environment, selected with: ibmicli env use NAME",
        (f"env = {_toml_str(cfg.active)}" if cfg.active else '# env = ""'),
        "",
        "# Extension -> member SRCTYPE overrides (default: uppercase extension).",
    ]
    if cfg.srctype:
        lines.append("[srctype]")
        lines += [f"{k} = {_toml_str(v)}" for k, v in sorted(cfg.srctype.items())]
    else:
        lines += ["# [srctype]", '# md = "TXT"']

    lines += [
        "",
        "# Download extensions by source file.",
        "# Default: QRPGLESRC=sqlrpgle, QCPYSRC/QPROTSRC=rpgle, QCLLESRC=clle, ...",
    ]
    if cfg.pull_ext:
        lines.append("[pull_ext]")
        lines += [f"{k} = {_toml_str(v)}" for k, v in sorted(cfg.pull_ext.items())]
    else:
        lines += ["# [pull_ext]", '# QRPGLESRC = "rpgle"']

    lines += [
        "",
        "# Environments configure the job library list. With setup commands,",
        "# ibmicli reads curlib and libl from IBM i; otherwise supply them here.",
        "# Create with 'ibmicli env set NAME'; update with 'ibmicli env refresh'.",
    ]
    for name in sorted(cfg.envs):
        env = cfg.envs[name]
        lines.append("")
        lines.append(f"[envs.{_toml_str(name)}]")
        if env.setup:
            lines.append(f"setup = [{', '.join(_toml_str(cmd) for cmd in env.setup)}]")
        lines.append(f"curlib = {_toml_str(env.curlib)}")
        lines.append(f"libl = [{', '.join(_toml_str(lib) for lib in env.libl)}]")

    lines += _build_lines(cfg)
    lines += _task_lines(cfg)
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
