"""Map compiler event diagnostics to original local source locations.

OPTION(*EVENTF) writes OBJLIB/EVFEVENT(OBJECT), containing FILEID, FILEEND,
EXPANSION and ERROR records. The SQL precompiler output QSQLTEMP1 includes
copies and generated SQL code. Remove EXPANSION ranges, then traverse the
FILEID/FILEEND copy tree to recover exact original lines. FILEID records use
parent-file copy lines; diagnostic columns remain unchanged. Without SQL,
compiler errors already name their original members.

CRTSQLRPGI OPTION(*EVENTF) cannot be combined with another OPTION inside
COMPILEOPT: duplicate OPTION parameters cause CPD0042 before RPG compilation.
An unchanged TIMESTAMP identifies stale events from a previous compilation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import copybook
from .config import Config
from .naming import InvalidName
from .project import Project

# Event source file in the object library.
FILE = "EVFEVENT"

# SQL precompiler processor and its output file ID.
PRECOMPILER = "999"


@dataclass
class Error:
    """Compiler error with its original IBM i member location."""

    msgid: str
    severity: int
    text: str
    member: str = ""  # 'LIB/FILE(MBR)'; empty when no location is reported.
    main: bool = False  # The compiled source, excluding copies.
    line: int = 0  # One-based; 0 means unknown.
    column: int = 0  # One-based; 0 means unknown.
    end_column: int = 0  # Last column for a single-line diagnostic.


@dataclass
class _Node:
    """File and copy children in the SQL precompiler expansion tree."""

    fileid: str
    name: str
    at: int  # Copy line in the parent; 0 for the root source.
    lines: int = 0
    children: list["_Node"] = field(default_factory=list)


def stamp(lines: list[str]) -> str:
    """Event member TIMESTAMP."""
    for line in lines:
        words = line.split()
        if words and words[0] == "TIMESTAMP":
            return words[-1]
    return ""


def parse(lines: list[str], min_severity: int) -> list[Error]:
    """Errors at or above min_severity, mapped to original members."""
    blocks: list[tuple[str, list[list[str]]]] = []
    for line in lines:
        words = line.split(None, 13)
        if not words:
            continue
        if words[0] == "PROCESSOR" and len(words) > 2:
            blocks.append((words[2], []))
        elif blocks:
            blocks[-1][1].append(words)

    tree, files, added = _precompiler(blocks)
    output_name = files.get(PRECOMPILER, "")

    errors: list[Error] = []
    seen: set[tuple[object, ...]] = set()
    for processor, records in blocks:
        names = _names(records)
        root = next((fileid for fileid in names if fileid != PRECOMPILER), "")
        for words in records:
            if words[0] != "ERROR" or len(words) < 13:
                continue
            severity = int(words[11])
            if severity < min_severity:
                continue
            error = Error(words[9], severity, (words[13] if len(words) > 13 else "").strip())
            line, column, end_line, end_column = (int(words[n]) for n in (5, 6, 7, 8))
            name = names.get(words[2], "")
            if line and name:
                # RPG diagnostics after SQL preprocessing refer to QSQLTEMP1;
                # map their expanded lines back. Other processors already
                # identify the original file.
                expanded = processor != PRECOMPILER and tree is not None and name == output_name
                where = _back(tree, files, added, line) if expanded else (words[2], name, line)
                # The compiled source is the first precompiler file,
                # or the first file of this processor when there is no precompiler.
                main = tree.fileid if tree is not None and (expanded or processor == PRECOMPILER) else root
                if where is not None:
                    fileid, error.member, error.line = where
                    error.main = fileid == main
                    error.column = column
                    error.end_column = end_column if end_line == line else 0
            key = (error.msgid, error.member, error.line, error.column, error.text)
            if key not in seen:
                seen.add(key)
                errors.append(error)
    return errors


def _names(records: list[list[str]]) -> dict[str, str]:
    return {words[2]: words[5] for words in records if words[0] == "FILEID" and len(words) > 5}


def _precompiler(
    blocks: list[tuple[str, list[list[str]]]],
) -> tuple[_Node | None, dict[str, str], list[tuple[int, int, str, int]]]:
    """Expanded source copy tree, file names and generated line ranges."""
    records = next((records for processor, records in blocks if processor == PRECOMPILER), None)
    if records is None:
        return None, {}, []
    root: _Node | None = None
    stack: list[_Node] = []
    added: list[tuple[int, int, str, int]] = []
    for words in records:
        if words[0] == "FILEID" and len(words) > 5 and words[2] != PRECOMPILER:
            node = _Node(words[2], words[5], int(words[3]))
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            stack.append(node)
        elif words[0] == "FILEEND" and len(words) > 3 and words[2] != PRECOMPILER and stack:
            stack.pop().lines = int(words[3])
        elif words[0] == "EXPANSION" and len(words) > 7 and words[5] == PRECOMPILER:
            added.append((int(words[6]), int(words[7]), words[2], int(words[3])))
    return root, _names(records), sorted(added)


def _back(
    tree: _Node, files: dict[str, str], added: list[tuple[int, int, str, int]], line: int
) -> tuple[str, str, int] | None:
    """Expanded line -> original file ID, member and line."""
    shift = 0
    for start, end, fileid, original in added:
        if end < line:
            shift += end - start + 1
        elif start <= line:
            # Generated SQL code maps to its original SQL statement.
            name = files.get(fileid, "")
            return (fileid, name, original) if name and original else None
        else:
            break
    return _inside(tree, line - shift)


def _inside(node: _Node, flat: int) -> tuple[str, str, int]:
    """Copy-expanded line -> original file ID, member and line."""
    consumed = 0  # Expanded lines already consumed.
    own = 0  # Original lines already consumed.
    for child in sorted(node.children, key=lambda child: child.at):
        before = child.at - own  # Include the copy directive itself.
        if flat <= consumed + before:
            return node.fileid, node.name, own + flat - consumed
        consumed += before
        own = child.at
        size = _size(child)
        if flat <= consumed + size:
            return _inside(child, flat - consumed)
        consumed += size
    return node.fileid, node.name, own + flat - consumed


def _size(node: _Node) -> int:
    return node.lines + sum(_size(child) for child in node.children)


def local_file(cfg: Config, error: Error, source: Path, project: Project | None = None) -> Path | None:
    """Existing local source or copy file, using the shared copybook resolution rules."""
    if not error.member:
        return None
    if error.main:
        return source
    try:
        found = copybook.resolve(cfg, project, copybook.parse(error.member), source)
    except (copybook.CopyError, InvalidName):
        return None
    return found.path if found.exists else None
