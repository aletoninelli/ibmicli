"""Extract standalone SQL from plain SQL or RPG exec sql statements.

Remove comments and exec sql. Strip host-variable INTO clauses from SELECT
and VALUES, turn DECLARE CURSOR into its query and SET :host into VALUES.
Host variables are replaced later with explicitly supplied SQL literals.
Statements that depend on program state are refused. This module neither
reads files nor opens connections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .naming import InvalidName


class NoStatement(InvalidName):
    """No executable statement at the requested location; a usage error (exit 2)."""


@dataclass
class Piece:
    """A source statement between unquoted semicolons."""

    text: str  # Without comments.
    start: int  # First nonempty line, one-based.
    end: int  # Line containing the semicolon or last text.


# RPG extensions use exec sql and // comments.
# Other sources are treated as plain SQL.
RPG = ("rpgle", "sqlrpgle", "rpgleinc", "sqlrpgleinc", "rpgleh")

EXEC = re.compile(r"^exec\s+sql\b\s*", re.IGNORECASE)

# These statements require program-local cursor, prepared-statement
# or diagnostics state.
REFUSED = frozenset({
    "open", "fetch", "close", "prepare", "execute", "describe", "whenever",
    "include", "allocate", "associate", "free", "connect", "disconnect",
    "release", "get", "signal", "resignal",
})

CURSOR = re.compile(
    r"^declare\s+[\w$#@\u00a3]+\s+(?:[a-z]+\s+)*?cursor\b"
    r"(?:\s+with(?:out)?\s+hold)?(?:\s+with(?:out)?\s+return(?:\s+to\s+(?:caller|client))?)?"
    r"\s+for\s+",
    re.IGNORECASE,
)
SET_HOST = re.compile(r"^set\s+:\s*[A-Za-z$#@_\u00a3][\w$#@.\u00a3]*\s*=\s*", re.IGNORECASE)
INTO_HOST = re.compile(r"\binto\s*:", re.IGNORECASE)
FROM = re.compile(r"\bfrom\b", re.IGNORECASE)
# Host variable: colon and a possibly qualified name, such as :data.field.
# A colon after a letter or another colon is not a host variable.
HOST = re.compile(r"(?<![\w$#@\u00a3:]):\s*([A-Za-z$#@_\u00a3][\w$#@\u00a3]*(?:\.[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)*)")


def is_rpg(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in RPG


def pieces(text: str, rpg: bool) -> list[Piece]:
    """Split statements with source locations, removing comments.

    Quoted semicolons do not end statements. Skip RPG directives, which
    have no semicolon and would otherwise join the following statement.
    """
    if rpg:
        text = "\n".join(_no_directive(number, line) for number, line in
                         enumerate(text.split("\n"), start=1))

    found: list[Piece] = []
    buffer: list[str] = []
    start = last = 0
    line = 1
    quote = ""
    i, size = 0, len(text)

    def close(end: int) -> None:
        nonlocal buffer, start
        body = "".join(buffer).strip()
        if body:
            found.append(Piece(body, start, end))
        buffer, start = [], 0

    while i < size:
        char = text[i]
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            elif char == "\n":
                line += 1
            i += 1
            continue
        if char == "\n":
            buffer.append(char)
            line += 1
            i += 1
            continue
        if text.startswith("--", i) or (rpg and text.startswith("//", i)):
            end = text.find("\n", i)
            i = size if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = size if end < 0 else end + 2
            line += text.count("\n", i, end)
            buffer.append(" ")
            i = end
            continue
        if char == ";":
            close(line)
            i += 1
            continue
        if char in "'\"":
            quote = char
        if not char.isspace():
            if not start:
                start = line
            last = line
        buffer.append(char)
        i += 1
    close(last)
    return found


def _no_directive(number: int, line: str) -> str:
    stripped = line.lstrip("\ufeff").lstrip()
    if number == 1 and stripped[:6].lower() == "**free":
        return ""
    if stripped.startswith("/") and not stripped.startswith(("//", "/*")):
        return ""
    return line


def at_line(text: str, line: int, rpg: bool) -> Piece:
    """Statement containing the one-based source line."""
    for piece in pieces(text, rpg):
        if piece.start <= line <= piece.end:
            if rpg and not EXEC.match(piece.text):
                raise NoStatement(f"no exec sql statement at line {line}")
            return piece
    raise NoStatement(f"no SQL statement at line {line}")


def prepare(sql: str) -> str:
    """Prepare standalone SQL, leaving host-variable replacement to substitute."""
    text = EXEC.sub("", sql.strip(), count=1).strip()
    if not text:
        raise NoStatement("'exec sql' without a statement")

    cursor = CURSOR.match(_mask(text))
    if cursor:
        text = text[cursor.end():].strip()
        if _first_word(text) not in ("select", "with", "values", "("):
            raise NoStatement(
                "cursor uses a prepared statement (for NAME): "
                "select and run the SQL text stored in that variable"
            )

    first = _first_word(text)
    if first in REFUSED or re.match(r"^set\s+option\b", text, re.IGNORECASE):
        word = "set option" if first == "set" else first
        raise NoStatement(
            f"'{word}' requires program state: "
            "run the cursor query or a complete statement"
        )

    host_set = SET_HOST.match(text)
    if host_set:
        text = "values " + text[host_set.end():].strip()
        first = "values"

    if first in ("select", "with", "values", "("):
        text = _without_into(text, first == "values")
    # Remove trailing blanks left by stripped comments.
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _without_into(text: str, values: bool) -> str:
    """Remove host-variable INTO through FROM in SELECT, or through the end in VALUES."""
    masked = _mask(text)
    into = INTO_HOST.search(masked)
    if into is None:
        return text
    if values:
        return text[: into.start()].rstrip()
    after = FROM.search(masked, into.end())
    if after is None:
        return text[: into.start()].rstrip()
    return text[: into.start()].rstrip() + " " + text[after.start():]


def host_variables(sql: str) -> list[str]:
    """Uppercase host variables in first-use order."""
    names: list[str] = []
    for found in HOST.finditer(_mask(sql)):
        name = found.group(1).upper()
        if name not in names:
            names.append(name)
    return names


def substitute(sql: str, values: dict[str, str]) -> str:
    """Replace host variables with SQL text from uppercase-keyed values.

    Missing values are reported before execution so a driver error cannot
    hide which host variable was omitted.
    """
    missing = [name for name in host_variables(sql) if name not in values]
    if missing:
        first = missing[0]
        raise NoStatement(
            "missing value for " + ", ".join(f":{name}" for name in missing) + "\n"
            f"       supply it with --var {first}=VALUE, written as SQL "
            f"(e.g. --var {first}=\"'A01'\" or --var {first}=123)"
        )
    parts: list[str] = []
    position = 0
    for found in HOST.finditer(_mask(sql)):
        parts.append(sql[position: found.start()])
        parts.append(values[found.group(1).upper()])
        position = found.end()
    parts.append(sql[position:])
    return "".join(parts)


def _mask(text: str) -> str:
    """Mask quoted contents without changing length so keyword searches ignore strings."""
    out: list[str] = []
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
                out.append(char)
            else:
                out.append(char if char == "\n" else "x")
        else:
            if char in "'\"":
                quote = char
            out.append(char)
    return "".join(out)


def _first_word(text: str) -> str:
    found = re.match(r"\s*(\(|[A-Za-z]+)", text)
    return found.group(1).lower() if found else ""
