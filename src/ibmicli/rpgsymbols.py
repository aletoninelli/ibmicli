"""Declarations in free-format RPGLE, including recursively resolved copies.

This supports VS Code completion and Outline. It reads declarations, nested
structures and their documentation comments, rather than compiling RPG.
Fixed-format declarations are not parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import copybook
from .config import Config
from .copybook import CopyError
from .project import Project

# Limit recursion depth as well as tracking visited files
# to protect against unusually long copy chains.
MAX_DEPTH = 10

DECLARATION = re.compile(
    r"^(?P<keyword>dcl-(?:proc|pr|pi|s|c|ds|f))\s+(?P<name>[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# dcl-subf and dcl-parm are optional but necessary for keyword names;
# strip them before reading the declared name.
FIELD = re.compile(
    r"^(?:dcl-(?:subf|parm)\s+)?(?P<name>[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
END = re.compile(r"^end-(proc|pr|pi|ds)\b", re.IGNORECASE)
# Subroutines are navigation targets in Outline,
# like procedures, even though they are not declarations.
BEGSR = re.compile(r"^begsr\s+(?P<name>[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)", re.IGNORECASE)
ENDSR = re.compile(r"^endsr\b", re.IGNORECASE)
# Procedure or program interface; its name can be *N.
PI = re.compile(r"^dcl-pi\s+(?:\*n\b|[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
# Structure template used to expose inherited fields
# for a likeds-qualified reference such as data.field.
LIKEDS = re.compile(r"\blikeds\s*\(\s*(?P<name>[A-Za-z$#@_\u00a3][\w$#@\u00a3]*)", re.IGNORECASE)

# The displayed type is the first useful word after the name,
# including its parameters.
TYPE = re.compile(
    r"^\s*(?P<type>"
    r"(?:char|varchar|packed|zoned|int|uns|float|ind|date|time|timestamp|pointer|"
    r"object|graph|vargraph|ucs2|varucs2|bindec|likeds|likerec|like|extname|"
    r"sqltype)\s*(?:\([^)]*\))?"
    r")",
    re.IGNORECASE,
)

KIND = {
    "dcl-proc": "procedure",
    "dcl-pr": "prototype",
    "dcl-s": "variable",
    "dcl-c": "constant",
    "dcl-ds": "struct",
    "dcl-f": "file",
}
SUBROUTINE = "subroutine"


@dataclass
class Symbol:
    """A declaration ready for completion display."""

    name: str
    kind: str
    detail: str = ""  # Short type, e.g. varchar(2000) or likeds(...).
    signature: str = ""  # Full declaration for hover text.
    doc: str = ""  # Preceding documentation comments.
    parent: str = ""  # Containing structure for subfields, e.g. 'a.b' when nested.
    like: str = ""  # Structure template from likeds, if present.
    params: list[dict[str, str]] = field(default_factory=list)
    file: str = ""
    line: int = 0
    # End of the block (end-ds, end-pr, end-proc, endsr); 0 means
    # a single statement or an unknown end.
    end_line: int = 0
    # Containing procedure for local declarations; empty for globals.
    scope: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "end_line": self.end_line,
            "scope": self.scope,
            "name": self.name,
            "kind": self.kind,
            "detail": self.detail,
            "signature": self.signature,
            "doc": self.doc,
            "parent": self.parent,
            "like": self.like,
            "params": self.params,
            "file": self.file,
            "line": self.line,
        }


@dataclass
class Statement:
    """One semicolon-terminated statement."""

    text: str
    line: int
    doc: list[str]
    trailing: str = ""  # Same-line comment after the semicolon.


def statements(text: str) -> list[Statement]:
    """Split free-format source into statements, retaining documentation comments.

    Declarations can span lines. Leading comments describe the next statement;
    trailing comments describe the statement that has just ended.
    """
    found: list[Statement] = []
    doc: list[str] = []
    buffer = ""
    start = 0

    for number, line in enumerate(text.splitlines(), start=1):
        # **free has no semicolon; skip it so it does not become part
        # of the first declaration.
        if number == 1 and line.lstrip("\ufeff")[:6].lower() == "**free":
            continue
        code, comment = _split_comment(line)

        # Directives (/copy, /if, /define, ...) also have no semicolon.
        # Skip them so they cannot consume the following declaration
        # or ctl-opt statement.
        if code.lstrip().startswith("/"):
            continue

        if not buffer and not code.strip():
            # A comment-only line documents what follows.
            # A blank line ends the documentation block so file headers
            # cannot accumulate on every declaration.
            if comment:
                doc.append(comment)
            elif not line.strip():
                doc = []
            continue

        closed = False
        # Ignore whitespace after the last semicolon on a line;
        # otherwise the next statement would start on the preceding
        # line instead of its actual first source line.
        while code.strip():
            head, separator, code = _split_statement(code)
            if not buffer:
                start = number
            buffer += head
            if not separator:
                buffer += " "
                break
            if buffer.strip():
                found.append(Statement(text=_squeeze(buffer), line=start, doc=doc))
                closed = True
            buffer = ""
            doc = []

        # Trailing comments describe the statement just closed,
        # including prototype parameters. Comments inside an unfinished
        # declaration are not attributed to another statement.
        if comment and closed and found:
            found[-1].trailing = comment

    if buffer.strip():
        found.append(Statement(text=_squeeze(buffer), line=start, doc=doc))
    return found


def _split_comment(line: str) -> tuple[str, str]:
    """Line -> code and comment, respecting quoted strings such as 'http://'."""
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "/" and line.startswith("//", index):
            return line[:index], line[index + 2:].strip()
        index += 1
    return line, ""


def _split_statement(code: str) -> tuple[str, bool, str]:
    """Code -> text before the first unquoted semicolon, found flag, remainder."""
    quoted = False
    for index, char in enumerate(code):
        if char == "'":
            quoted = not quoted
        elif char == ";" and not quoted:
            return code[:index], True, code[index + 1:]
    return code, False, ""


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def declarations(text: str, source: str = "") -> list[Symbol]:
    """Local declarations, without following copies.

    dcl-pr contents are parameters included in its signature. dcl-ds
    contents are named subfields available for completion.
    """
    symbols: list[Symbol] = []
    block: Symbol | None = None  # Open declaration block (dcl-pr / dcl-ds / dcl-proc).
    lines: list[str] = []  # Signature lines of the open block.
    # Containing structures: nested qualified dcl-ds blocks
    # have subfields that can themselves have subfields.
    outer: list[tuple[Symbol, list[str]]] = []
    # Outline state: current procedure, current subroutine,
    # and whether the procedure interface is open.
    # Only interface contents are procedure parameters.
    proc: Symbol | None = None
    routine: Symbol | None = None
    in_pi = False

    def close() -> None:
        nonlocal block, lines
        if block is not None and lines:
            block.signature = "\n".join(lines)
        block, lines = None, []
        outer.clear()

    for statement in statements(text):
        body = statement.text
        end = END.match(body)
        if end:
            which = end.group(1).lower()
            if which == "proc" and proc is not None:
                proc.end_line = statement.line
                proc = None
            elif which == "pi":
                in_pi = False
            elif block is not None:
                block.end_line = statement.line
            if block is not None and outer:
                lines.append(f"{body};")
                block.signature = "\n".join(lines)
                inner = lines
                block, lines = outer.pop()
                lines.extend(f"  {line}" for line in inner)
            elif block is not None:
                lines.append(f"{body};")
                close()
            continue

        subroutine = BEGSR.match(body)
        if subroutine:
            close()
            routine = Symbol(
                name=subroutine.group("name"),
                kind=SUBROUTINE,
                signature=f"{body};",
                doc=_doc(statement),
                file=source,
                line=statement.line,
                scope=proc.name if proc is not None else "",
            )
            symbols.append(routine)
            continue
        if ENDSR.match(body):
            if routine is not None:
                routine.end_line = statement.line
                routine = None
            continue

        interface = PI.match(body)
        if interface:
            # The procedure interface supplies its signature without creating
            # a duplicate symbol. Parse it before DECLARATION, since *N is
            # not a declared name and would otherwise be misread
            # as a parameter named dcl.
            if block is not None and block.kind == "procedure":
                block.detail = block.detail or _type_of(interface.group("rest").strip())
                lines.append(f"{body};")
                in_pi = not _self_contained("dcl-pi", body)
            else:
                # Program interface parameters are not separate symbols.
                close()
            continue

        found = DECLARATION.match(body)
        if found:
            keyword = found.group("keyword").lower()
            rest = found.group("rest").strip()

            if keyword == "dcl-ds" and block is not None and block.kind in ("struct", "subfield"):
                nested = Symbol(
                    name=found.group("name"),
                    kind="subfield",
                    detail=_type_of(rest),
                    signature=f"{body};",
                    doc=_doc(statement),
                    parent=_path(block),
                    like=_like(rest),
                    file=source,
                    line=statement.line,
                )
                symbols.append(nested)
                if _self_contained(keyword, body):
                    lines.append(f"  {body};")
                else:
                    outer.append((block, lines))
                    block, lines = nested, [f"{body};"]
                continue

            close()
            symbol = Symbol(
                name=found.group("name"),
                kind=KIND[keyword],
                detail=_type_of(rest),
                signature=f"{body};",
                doc=_doc(statement),
                like=_like(rest) if keyword == "dcl-ds" else "",
                file=source,
                line=statement.line,
                scope=proc.name if proc is not None and keyword != "dcl-proc" else "",
            )
            symbols.append(symbol)
            if keyword == "dcl-proc":
                proc, in_pi = symbol, False
            if keyword in ("dcl-pr", "dcl-ds", "dcl-proc") and not _self_contained(keyword, body):
                block, lines = symbol, [f"{body};"]
            continue

        if block is None:
            continue
        if block.kind == "procedure" and not in_pi:
            # Without dcl-pi, subsequent statements are procedure code,
            # not parameters.
            close()
            continue

        field_found = FIELD.match(body)
        if not field_found:
            continue
        rest = field_found.group("rest").strip()
        lines.append(f"  {body};" + (f"  // {statement.trailing}" if statement.trailing else ""))
        if block.kind in ("prototype", "procedure"):
            block.params.append({
                "name": field_found.group("name"),
                "type": _type_of(rest),
                "doc": statement.trailing,
            })
        elif block.kind in ("struct", "subfield"):
            symbols.append(
                Symbol(
                    name=field_found.group("name"),
                    kind="subfield",
                    detail=_type_of(rest),
                    signature=f"{body};",
                    doc=_doc(statement),
                    parent=_path(block),
                    like=_like(rest),
                    file=source,
                    line=statement.line,
                )
            )

    close()
    return symbols


def _path(symbol: Symbol) -> str:
    """Parent path used by a symbol's subfields."""
    return f"{symbol.parent}.{symbol.name}" if symbol.parent else symbol.name


def _like(rest: str) -> str:
    found = LIKEDS.search(rest)
    return found.group("name") if found else ""


def _doc(statement: Statement) -> str:
    """Leading and trailing documentation for a statement."""
    lines = [*statement.doc, statement.trailing]
    return "\n".join(line for line in lines if line).strip()


def _self_contained(keyword: str, body: str) -> bool:
    """Whether a declaration ends without opening a block.

    Inline end-ds and likeds declarations have no local subfields to read.
    """
    if re.search(r"\bend-(pr|ds|pi)\b", body, re.IGNORECASE):
        return True
    if keyword == "dcl-ds":
        return bool(re.search(r"\b(likeds|likerec|extname)\s*\(", body, re.IGNORECASE))
    return False


def _type_of(rest: str) -> str:
    found = TYPE.match(rest)
    if found:
        return _squeeze(found.group("type"))
    # Unrecognized type text can be a constant value; retain it.
    return _squeeze(rest.split(" inz")[0])[:60]


def with_problems(
    cfg: Config,
    path: Path,
    only_copies: bool = False,
    text: str | None = None,
    follow: bool = True,
    project: Project | None = None,
) -> tuple[list[Symbol], list[dict[str, object]]]:
    """Visible symbols and problems encountered while reading copies.

    only_copies avoids duplicating declarations already provided by a local
    language extension. text can supply an unsaved editor buffer; copies
    are still read from disk. follow=False reads only the root source.
    Unresolved copies are recorded without hiding other declarations.
    """
    return _walk(cfg, project, path.resolve(), only_copies=only_copies, text=text, follow=follow)


def _walk(
    cfg: Config, project: Project | None, path: Path, only_copies: bool,
    text: str | None = None, follow: bool = True,
) -> tuple[list[Symbol], list[dict[str, object]]]:
    symbols: list[Symbol] = []
    problems: list[dict[str, object]] = []
    for current, content, depth in _sources(cfg, project, path, problems, text, follow):
        if depth or not only_copies:
            symbols.extend(declarations(content, source=str(current)))
    return symbols, problems


def _sources(
    cfg: Config,
    project: Project | None,
    path: Path,
    problems: list[dict[str, object]],
    root_text: str | None = None,
    follow: bool = True,
) -> list[tuple[Path, str, int]]:
    """Recursively collect (path, text, depth), recording unreadable copies."""
    found: list[tuple[Path, str, int]] = []
    seen: set[Path] = set()

    def visit(current: Path, depth: int) -> None:
        resolved = current.resolve()
        if resolved in seen or depth > MAX_DEPTH:
            return
        seen.add(resolved)
        if depth == 0 and root_text is not None:
            text = root_text
        else:
            try:
                text = resolved.read_text(encoding=cfg.local_charset, errors="replace")
            except OSError:
                return

        found.append((resolved, text, depth))
        if not follow:
            return

        for spec in copybook.scan(text):
            try:
                target = copybook.resolve(cfg, project, spec, resolved)
            except CopyError as exc:
                problems.append(
                    {"copy": spec.text, "file": str(resolved), "line": spec.line,
                     "error": str(exc).splitlines()[0]}
                )
                continue
            if not target.exists:
                problems.append(
                    {"copy": spec.text, "file": str(resolved), "line": spec.line,
                     "error": f"not downloaded: missing {target.path}"}
                )
                continue
            visit(target.path, depth + 1)

    visit(path, 0)
    return found


# --- NOMAIN ----------------------------------------------------------------

CTL_OPT = re.compile(r"^ctl-opt\b", re.IGNORECASE)
NOMAIN = re.compile(r"\bnomain\b", re.IGNORECASE)
# Fixed-format control specification: H in column 6,
# without a comment asterisk in column 7.
H_SPEC = re.compile(r"^.{5}[Hh](?!\*)")


def is_nomain(cfg: Config, path: Path, project: Project | None = None) -> bool:
    """Whether a single-module object must be a service program.

    A NOMAIN module has no entry point and cannot form a program by itself.
    Follow readable copies because control options can be declared there.
    """
    for _, text, _ in _sources(cfg, project, path.resolve(), []):
        # In **free sources, columns are insignificant: an indented h
        # must not be mistaken for a fixed-format control specification.
        fixed = not text.lstrip("\ufeff")[:6].lower() == "**free"
        for line in text.splitlines() if fixed else ():
            if H_SPEC.match(line) and NOMAIN.search(line[6:]):
                return True
        for statement in statements(text):
            if CTL_OPT.match(statement.text) and NOMAIN.search(statement.text):
                return True
    return False
