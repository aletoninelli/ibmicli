"""Interactive shell using the same Typer application as the CLI.

ibmicli commands reuse one FTP session. Unknown commands, pipes and
redirections go unchanged to the local shell. No separate command registry
is needed; newly registered CLI commands are available here automatically.
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from . import config as cfgmod
from . import output
from . import session as sessions
from .config import Config, ConfigError

EXIT_WORDS = {"exit", "quit", "bye", "q"}
HELP_WORDS = {"help", "?"}
HISTORY_LINES = 1000

# Prevent nested shells that would require multiple exits.
_active = False


def active() -> bool:
    return _active


def run(app: Any) -> int:
    global _active
    if _active:
        output.error("already in the shell; use 'exit' to leave")
        return 2
    _active = True
    try:
        return Shell(app).loop()
    finally:
        _active = False


class Shell:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.commands = _commands(app)
        self.cfg: Config | None = None
        # When credentials change inside the shell,
        # replace the pinned FTP session.
        self.connected_as: tuple[str, str, str] | None = None

    def loop(self) -> int:
        self._refresh()
        self._banner()
        readline = _setup_readline(self.commands)
        try:
            while True:
                try:
                    line = input(self._prompt())
                except EOFError:  # Ctrl-D exits.
                    print()
                    break
                except KeyboardInterrupt:  # Ctrl-C cancels the current line.
                    print("^C")
                    continue

                args = _split(line)
                if not args:
                    continue
                if args[0].lower() in EXIT_WORDS:
                    break

                self._run(line, args)
                self._refresh()  # env use and set-conn update the prompt.
        finally:
            _save_history(readline)
            sessions.unpin()
        return 0

    # --- State -------------------------------------------------------------

    def _refresh(self) -> None:
        """Reload environment prompt state and connection settings."""
        try:
            self.cfg = cfgmod.load()
        except ConfigError:
            self.cfg = None

        current = (
            (self.cfg.host, self.cfg.user, self.cfg.password) if self.cfg else None
        )
        if current != self.connected_as:
            sessions.unpin()
            if self.cfg is not None:
                sessions.pin(self.cfg)
            self.connected_as = current

    def _prompt(self) -> str:
        env = self.cfg.env() if self.cfg else None
        return f"ibmicli {env.name}> " if env else "ibmicli> "

    def _banner(self) -> None:
        print(f"ibmicli {__version__} - interactive shell")
        if self.cfg is None:
            output.error("missing or incomplete configuration: run 'set-conn' here")
        else:
            print(f"connection: {self.cfg.user}@{self.cfg.host} (opens on the first command)")
            env = self.cfg.env()
            name = env.name if env else "none (env use NAME)"
            print(f"environment:     {name}")
        print("'help' for ibmicli; other commands run locally; 'exit' to leave.")

    # --- Execution ---------------------------------------------------------

    def _run(self, line: str, args: list[str]) -> int:
        stripped = line.lstrip()
        if stripped.startswith("!"):
            command = stripped[1:].lstrip()
            return self._run_local(command, _split(command)) if command else 0

        original = args
        args = _normalize(args)
        if not args:
            return 0
        first = args[0].lower()
        explicit_ibmicli = bool(original and original[0].lower() == "ibmicli")
        explicit_help = bool(original and original[0].lower() in HELP_WORDS)
        if not explicit_ibmicli and not explicit_help and first not in self.commands:
            return self._run_local(line, original)

        return self._run_ibmicli(args)

    def _run_ibmicli(self, args: list[str]) -> int:
        try:
            # standalone_mode preserves CLI help and error output.
            # Typer exits with SystemExit, treated here as the return
            # status of one shell command.
            self.app(args=args, prog_name="ibmicli", standalone_mode=True)
        except SystemExit as exc:
            return _exit_code(exc.code)
        except KeyboardInterrupt:
            print("^C")
            return 1
        except Exception as exc:
            # A failing command must not terminate the interactive shell.
            if os.environ.get("IBMICLI_DEBUG"):
                traceback.print_exc()
            output.error(f"{type(exc).__name__}: {exc}")
            return 1
        return 0

    def _run_local(self, line: str, args: list[str]) -> int:
        """Execute locally; a standalone cd also changes the ibmicli working folder."""
        if not args:
            return 0
        if args[0] == "cd" and len(args) <= 2 and _simple_cd(line):
            return _change_dir(args[1] if len(args) == 2 else None)

        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            return subprocess.run(line, shell=True, executable=shell, check=False).returncode
        except KeyboardInterrupt:
            print("^C")
            return 130
        except OSError as exc:
            output.error(f"local shell unavailable: {exc}")
            return 1


def _split(line: str) -> list[str]:
    if not line.strip():
        return []
    try:
        return shlex.split(line)
    except ValueError as exc:
        output.error(f"cannot parse line: {exc}")
        return []


def _normalize(args: list[str]) -> list[str]:
    """Normalize interactive shorthand into CLI arguments."""
    if args and args[0].lower() == "ibmicli":
        args = args[1:]  # Allow a habitually typed ibmicli prefix.
    if not args:
        return []
    if args[0].lower() in HELP_WORDS:
        rest = args[1:]  # 'help pull' -> 'pull --help'
        return [*rest, "--help"]
    if len(args) > 1 and args[-1].lower() in HELP_WORDS:
        # 'pull help' -> 'pull --help' for CLI commands only.
        # Local commands such as 'git help' never pass through this normalization.
        return [*args[:-1], "--help"]
    return args


def _exit_code(code: object) -> int:
    if isinstance(code, int):
        return code
    return 0 if code is None else 1


def _simple_cd(line: str) -> bool:
    """Whether cd has no operators requiring a separate shell."""
    return not any(marker in line for marker in (";", "&", "|", "<", ">", "`", "$("))


def _change_dir(target: str | None) -> int:
    """Change the process folder so the next command sees it."""
    previous = str(Path.cwd())
    show_path = target == "-"
    if target is None:
        destination = str(Path.home())
    elif show_path:
        destination = os.environ.get("OLDPWD", "")
        if not destination:
            output.error("cd: OLDPWD is not set")
            return 1
    else:
        destination = os.path.expandvars(os.path.expanduser(target))

    try:
        os.chdir(destination)
    except OSError as exc:
        output.error(f"cd: {target or destination}: {exc.strerror or exc}")
        return 1

    current = str(Path.cwd())
    os.environ["OLDPWD"] = previous
    os.environ["PWD"] = current
    if show_path:
        print(current)
    return 0


def _commands(app: Any) -> dict[str, list[str]]:
    """Commands and options for TAB completion."""
    try:
        from typer.main import get_command

        group = get_command(app)
    except Exception:
        return {}

    commands = {name: [] for name in EXIT_WORDS | HELP_WORDS}
    for name, command in getattr(group, "commands", {}).items():
        options = {opt for param in command.params for opt in param.opts if opt.startswith("-")}
        commands[name] = sorted(options)
    return commands


# --- Readline: history, arrows, TAB ----------------------------------------
# Readline is optional; the shell still works
# without editing and completion support.


def _history_path() -> Path:
    return cfgmod.config_path().parent / "history"


def _matches(commands: dict[str, list[str]], before: str, text: str) -> list[str]:
    """Completion candidates for the text before the cursor.

    Kept independent of readline for testing. Never query IBM i: TAB must
    respond immediately. list provides remote member names separately.
    """
    words = before.split()
    if not words:  # First word: command name.
        return [name + " " for name in sorted(commands) if name.startswith(text)]
    if text.startswith("-"):  # Options of the current command.
        return [opt + " " for opt in commands.get(words[0].lower(), []) if opt.startswith(text)]
    return _local_paths(text)  # Argument: local paths.


def _local_paths(text: str) -> list[str]:
    """Local files and folders starting with text, relative to the current folder."""
    home = os.path.expanduser("~")
    matches = []
    for path in sorted(glob.glob(os.path.expanduser(text) + "*")):
        shown = "~" + path[len(home):] if text.startswith("~") and path.startswith(home) else path
        # Folders end with / and no space so completion can descend further.
        matches.append(shown + ("/" if os.path.isdir(path) else " "))
    return matches


def _setup_readline(commands: dict[str, list[str]]) -> Any:
    try:
        import readline
    except ImportError:
        return None

    try:
        readline.read_history_file(_history_path())
    except OSError:
        pass
    readline.set_history_length(HISTORY_LINES)

    def complete(text: str, state: int) -> str | None:
        found = _matches(commands, readline.get_line_buffer()[: readline.get_begidx()], text)
        return found[state] if state < len(found) else None

    readline.set_completer(complete)
    # Keep / and . inside words so path completion sees
    # the complete prefix rather than only the last component.
    readline.set_completer_delims(" \t\n")
    # macOS uses libedit, which has different binding syntax from GNU readline.
    doc = getattr(readline, "__doc__", "") or ""
    readline.parse_and_bind("bind ^I rl_complete" if "libedit" in doc else "tab: complete")
    return readline


def _save_history(readline: Any) -> None:
    if readline is None:
        return
    try:
        path = _history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(path)
    except OSError:
        pass
