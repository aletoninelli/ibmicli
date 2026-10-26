"""Resolve copy directives and optionally download missing members."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

from .. import copybook, output, vscode
from ..config import Config
from ..copybook import CopyError, Resolved, Spec
from ..naming import InvalidName
from ..project import Project
from . import pull as pull_cmd


def run(
    cfg: Config,
    specs: list[str],
    origin: Path,
    everything: bool = False,
    pull: bool = True,
    open_editor: bool = False,
    as_json: bool = False,
    dry_run: bool = False,
    project: Project | None = None,
) -> int:
    """Return 0 if all copies resolve, 1 for missing or failed copies."""
    # Only JSON belongs on stdout in JSON mode;
    # redirect output from pull to stderr.
    with contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext():
        results = _collect(cfg, project, specs, origin, everything, pull, dry_run)

    if as_json:
        print(json.dumps({"from": str(origin), "copies": results}, ensure_ascii=False))
    elif open_editor:
        _open([r for r in results if r.get("exists")])

    # Dry-run does not create files, so an absent file is expected.
    missing = any(r.get("error") or (not r.get("exists") and not dry_run) for r in results)
    return 1 if missing else 0


def _collect(
    cfg: Config,
    project: Project | None,
    specs: list[str],
    origin: Path,
    everything: bool,
    pull: bool,
    dry_run: bool,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for spec in _specs(cfg, specs, origin, everything):
        results.append(_one(cfg, project, spec, origin, pull, dry_run))
    return results


def _specs(cfg: Config, specs: list[str], origin: Path, everything: bool) -> list[Spec]:
    if not everything:
        found = []
        for text in specs:
            try:
                found.append(copybook.parse(text))
            except InvalidName as exc:
                output.skip(f"{text}  ({exc})")
        return found

    try:
        text = origin.read_text(encoding=cfg.local_charset, errors="replace")
    except OSError as exc:
        raise CopyError(f"{origin}: {exc}") from exc
    return copybook.scan(text)


def _one(
    cfg: Config, project: Project | None, spec: Spec, origin: Path, pull: bool, dry_run: bool
) -> dict[str, object]:
    entry: dict[str, object] = {
        "copy": spec.text,
        "line": spec.line,
        "start": spec.start,
        "end": spec.end,
    }
    try:
        found = copybook.resolve(cfg, project, spec, origin)
    except (CopyError, InvalidName) as exc:
        message = str(exc)
        entry["error"] = message.splitlines()[0]
        entry["exists"] = False
        output.fail(spec.text)
        output.note(message)
        return entry

    entry.update(
        {
            "library": found.library,
            "srcfile": found.srcfile,
            "member": found.member,
            "remote": found.remote,
            "path": str(found.path),
            "exists": found.exists,
            "pulled": False,
            "error": None,
        }
    )

    if found.exists:
        output.ok(f"{found.remote}  ->  {found.path}")
        return entry

    if not pull:
        output.skip(f"{found.remote}  (missing: {found.path})")
        return entry

    entry["pulled"] = _pull(cfg, found, dry_run)
    entry["exists"] = found.path.is_file()
    if not entry["exists"] and not dry_run:
        entry["error"] = f"{found.remote}: pull failed"
    return entry


def _pull(cfg: Config, found: Resolved, dry_run: bool) -> bool:
    """Download to the resolved folder, creating it if necessary."""
    if not dry_run:
        found.path.parent.mkdir(parents=True, exist_ok=True)
    failures = pull_cmd.run(
        cfg,
        found.library,
        found.srcfile,
        [found.member],
        found.path.parent,
        dry_run=dry_run,
        force=False,
    )
    return failures == 0


def _open(results: list[dict[str, object]]) -> None:
    """Open resolved copies in VS Code or report its absence."""
    if not results:
        return
    code = vscode.find_code()
    if code is None:
        output.fail("Visual Studio Code not found: open the file manually")
        return
    try:
        subprocess.run([str(code), *(str(r["path"]) for r in results)], check=False)
    except OSError as exc:
        output.fail("could not open VS Code")
        output.note(str(exc))
