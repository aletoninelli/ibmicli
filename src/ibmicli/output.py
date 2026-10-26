"""Result lines: successful output on stdout, problems on stderr."""

from __future__ import annotations

import sys

_COLORS = {
    "OK": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m", "DRY": "\033[36m",
    "DIFF": "\033[33m", "LOCAL": "\033[36m", "IBMI": "\033[36m",
}
_RESET = "\033[0m"


def _emit(tag: str, message: str, stream) -> None:
    label = f"{tag:<5}" if tag else "     "
    color = _COLORS.get(tag)
    if color and stream.isatty():
        label = f"{color}{label}{_RESET}"
    print(f"{label} {message}", file=stream)


def ok(message: str) -> None:
    _emit("OK", message, sys.stdout)


def dry(message: str) -> None:
    _emit("DRY", message, sys.stdout)


def skip(message: str) -> None:
    _emit("SKIP", message, sys.stderr)


def fail(message: str) -> None:
    _emit("FAIL", message, sys.stderr)


def note(message: str) -> None:
    """Indented details for a preceding result, including multiline FTP responses."""
    for line in str(message).splitlines() or [""]:
        _emit("", f" {line.strip()}", sys.stderr)


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def info(text: str) -> None:
    """Context before an operation, such as project or library names."""
    print(text)


def command(text: str) -> None:
    """CL command being sent to IBM i."""
    print(f"> {text}")


def message(text: str) -> None:
    """IBM i response line, as in a 5250 session."""
    print(f"  {text}")


def tagged(tag: str, message: str) -> None:
    """Status result on stdout with a DIFF, LOCAL or IBMI tag."""
    _emit(tag, message, sys.stdout)


# Git-style diff colors.
_DIFF_COLORS = {"+": "\033[32m", "-": "\033[31m", "@": "\033[36m"}


def diff(line: str) -> None:
    """One line of a member-to-local-file comparison."""
    color = _DIFF_COLORS.get(line[:1])
    if line.startswith(("+++", "---")):
        color = "\033[1m"
    if color and sys.stdout.isatty():
        line = f"{color}{line}{_RESET}"
    print(line)


def progress(text: str) -> None:
    """Update one progress line on terminals only; omit it in pipes and files."""
    if sys.stderr.isatty():
        print(f"\r\033[K{text}", end="", file=sys.stderr, flush=True)


def progress_end() -> None:
    """Clear the progress line before writing other output."""
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr, flush=True)


def problem(text: str) -> None:
    """Compiler diagnostic header, on stdout to preserve response order."""
    if sys.stdout.isatty():
        text = f"{_COLORS['FAIL']}{text}{_RESET}"
    print(f"  {text}")
