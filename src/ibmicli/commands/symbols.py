"""List RPG symbols from a source and its recursive copies.

Unreadable copies are reported without hiding other symbols. -local limits
parsing to the root source; --stdin supplies an unsaved editor buffer for Outline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import output, rpgsymbols
from ..config import Config
from ..project import Project

# Displayed columns: kind, name, data type.
WIDTH = 22


def run(
    cfg: Config,
    source: Path,
    only_copies: bool = False,
    as_json: bool = False,
    kind: str | None = None,
    local: bool = False,
    stdin_text: bool = False,
    project: Project | None = None,
) -> int:
    if only_copies and local:
        output.error("-copies and -local cannot be combined: choose copies or the source")
        return 2
    # With --stdin the file may not exist yet; its path still
    # locates the project and copies.
    if not stdin_text and not source.is_file():
        output.error(f"{source}: file not found")
        return 2

    text = sys.stdin.read() if stdin_text else None
    symbols, problems = rpgsymbols.with_problems(
        cfg, source, only_copies=only_copies, text=text, follow=not local, project=project
    )
    if kind:
        symbols = [symbol for symbol in symbols if symbol.kind == kind.lower()]

    if as_json:
        print(
            json.dumps(
                {
                    "file": str(source.resolve()),
                    "symbols": [symbol.as_dict() for symbol in symbols],
                    "problems": problems,
                },
                ensure_ascii=False,
            )
        )
        return 0

    for symbol in symbols:
        where = Path(symbol.file).name
        name = f"{symbol.parent}.{symbol.name}" if symbol.parent else symbol.name
        print(f"{symbol.kind:<10} {name:<{WIDTH}} {symbol.detail:<24} {where}:{symbol.line}")

    if problems:
        for problem in problems:
            output.skip(f"/copy {problem['copy']}  ({problem['error']})")
        proj = f" -proj {project.path}" if project is not None else ""
        output.note(f"download them with: ibmicli copy -from {source} -all -pull{proj}")
    return 1 if problems else 0
