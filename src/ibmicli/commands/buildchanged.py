"""Upload and build Git-changed sources below the current folder.

Changes are differences from HEAD or the merge base with --since, plus
untracked files. No upload ledger is written; unchanged uncommitted files
are uploaded again on another run. Upload all copies and sources first,
then build service programs before programs. NOMAIN selection assumes one
module per object. Build mappings select tasks by source file.

--json returns only a plan. Upload failures prevent that file's builton
while other files continue; any failure produces exit code 1.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import output, project as prj, rpgsymbols, session as sessions
from ..config import Config, ConfigError
from ..copybook import CopyError
from ..naming import InvalidName, split_file
from . import deploy as deploy_cmd, run as run_cmd

PGM, SRV = "*PGM", "*SRVPGM"


@dataclass
class Item:
    """Changed file and planned action."""

    path: Path
    library: str
    srcfile: str
    kind: str = ""  # PGM, SRV or empty for upload only.
    task: str = ""
    note: str = ""  # Optional reason for object type selection.


def run(
    cfg: Config,
    since: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    app: Any = None,
    as_json: bool = False,
    project: prj.Project | None = None,
) -> int:
    if project is None:
        # Require the project explicitly: otherwise every file could be ignored
        # and a misleading empty plan would hide the missing configuration.
        raise prj.no_project()
    if cfg.build_problems:
        raise ConfigError(
            f"{cfg.path}: invalid [deploy-build-changed] section:\n       "
            + "\n       ".join(cfg.build_problems)
        )

    changed = _changed(since)
    items, skipped, ignored = _classify(cfg, project, changed)

    # Validate task names before uploading any sources.
    available, _ = prj.tasks_for(cfg, project)
    for name in sorted({item.task for item in items if item.task}):
        cfg.require_task(name, available)

    if as_json:
        print(json.dumps(_as_dict(items, skipped, ignored), ensure_ascii=False))
        return 0

    _plan(items, skipped, ignored, since)
    if not items:
        return 1 if skipped else 0

    failures = len(skipped)
    shared = not dry_run and not sessions.is_pinned()
    if shared:
        sessions.pin(cfg)
    try:
        sent = _deploy(cfg, items, dry_run)
        failures += len(items) - len(sent)
        for kind in (SRV, PGM):
            for item in sent:
                if item.kind == kind and not _build(cfg, project, item, dry_run, verbose, app):
                    failures += 1
    finally:
        if shared:
            sessions.unpin()

    output.info("")
    if dry_run:
        output.dry(f"{len(items)} files to upload, {sum(1 for i in items if i.kind)} to build")
        return 1 if failures else 0
    if failures:
        output.fail(f"{failures} files failed: see above")
        return 1
    output.ok(f"{len(items)} files uploaded, {sum(1 for i in items if i.kind)} built")
    return 0


# --- Changed files ---------------------------------------------------------


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise ConfigError(f"git unavailable: {exc}") from exc
    if done.returncode != 0:
        raise ConfigError(f"git {args[0]}: {done.stderr.strip() or 'error'}")
    return done.stdout


def _changed(since: str | None) -> list[Path]:
    """Changed paths below the current folder, relative to it.

    Compare with the merge base, not REF itself: changes added to REF after
    branching must not look like local edits and trigger outdated uploads.
    """
    if _git("rev-parse", "--is-inside-work-tree").strip() != "true":
        raise ConfigError("current folder is not inside a Git repository")
    base = _git("merge-base", since, "HEAD").strip() if since else "HEAD"
    tracked = _git("diff", "--name-only", "--relative", "-z", base, "--", ".")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z", "--", ".")
    names = {name for name in (tracked + untracked).split("\0") if name}
    return sorted(Path(name) for name in names)


def _classify(
    cfg: Config, project: prj.Project, changed: list[Path]
) -> tuple[list[Item], list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Classify actionable, failed and ignored paths.

    Skipped paths count as failures; deleted paths and files outside known source folders do not.
    """
    known = cfg.known_srcfiles
    items: list[Item] = []
    skipped: list[tuple[Path, str]] = []
    ignored: list[tuple[Path, str]] = []

    for path in changed:
        if not path.is_file():
            ignored.append((path, "deleted locally: remote member is retained"))
            continue
        try:
            try:
                target = prj.resolve(cfg, project, path)
            except ConfigError:
                # An unmapped folder is an error only when it contains
                # a known source file; template/rpgle need not be an app.
                if path.parent.name.upper() not in known:
                    ignored.append((path, "outside APP/LIBRARY/SOURCEFILE"))
                    continue
                raise
            if target is None:
                ignored.append((path, "outside APP/LIBRARY/SOURCEFILE"))
                continue
            if target.srcfile not in known:
                ignored.append((path, f"{target.srcfile} is not a known source file"))
                continue
            _, ext = split_file(path)
            item = Item(path, prj.source_library(target), target.srcfile)
            _choose(cfg, project, item, ext)
        except (ConfigError, InvalidName, CopyError) as exc:
            skipped.append((path, str(exc).splitlines()[0]))
            continue
        items.append(item)
    return items, skipped, ignored


def _choose(cfg: Config, project: prj.Project, item: Item, ext: str) -> None:
    """Select object kind and task, or leave empty for upload only."""
    build = cfg.build.get(item.srcfile)
    if build is None:
        return
    if build.pgm == build.srv:
        # A single task for this source file does not need NOMAIN selection.
        item.kind, item.task = PGM, build.pgm
    else:
        # An unreadable copy containing NOMAIN can cause *PGM classification.
        # CRTPGM will report the missing entry point, so this cannot
        # silently create the wrong object.
        nomain = rpgsymbols.is_nomain(cfg, item.path, project)
        item.kind = SRV if nomain else PGM
        item.task = build.srv if nomain else build.pgm
        item.note = "nomain" if nomain else ""
        if not item.task:
            key = "srv" if nomain else "pgm"
            raise InvalidName(
                f"{item.kind} requires '{key}' in [deploy-build-changed] {item.srcfile}"
            )
    task = cfg.require_task(item.task, prj.tasks_for(cfg, project)[0])
    if not task.accepts(ext):
        raise InvalidName(f"task '{task.name}' does not accept .{ext}")


# --- Display ---------------------------------------------------------------


def _plan(
    items: list[Item],
    skipped: list[tuple[Path, str]],
    ignored: list[tuple[Path, str]],
    since: str | None,
) -> None:
    comparison = f"relative to {since}" if since else "relative to the last commit"
    if not items and not skipped:
        output.info(f"no changed sources {comparison}")
    else:
        output.info(f"{len(items) + len(skipped)} changed sources {comparison}:")
    width = max((len(str(item.path)) for item in items), default=0)
    for kind in ("", SRV, PGM):
        for item in items:
            if item.kind != kind:
                continue
            label = item.kind or "upload"
            line = f"  {label:<8} {str(item.path):<{width}}"
            if item.task:
                line += f"  {item.task}"
            if item.note:
                line += f"  ({item.note})"
            output.info(line)
    for path, reason in skipped:
        output.skip(f"{path}  ({reason})")
    for path, reason in ignored:
        output.info(f"  ignored {path}  ({reason})")


def _as_dict(
    items: list[Item],
    skipped: list[tuple[Path, str]],
    ignored: list[tuple[Path, str]],
) -> dict[str, object]:
    """VS Code plan in execution order."""
    order = {"": 0, SRV: 1, PGM: 2}
    # Keep the upload-only kind value stable for JSON consumers.
    return {
        "items": [
            {"file": str(item.path), "kind": item.kind or "invio", "task": item.task,
             "note": item.note}
            for item in sorted(items, key=lambda item: order[item.kind])
        ],
        "skipped": [{"file": str(path), "reason": reason} for path, reason in skipped],
        "ignored": [{"file": str(path), "reason": reason} for path, reason in ignored],
    }


# --- Execution -------------------------------------------------------------


def _deploy(cfg: Config, items: list[Item], dry_run: bool) -> list[Item]:
    """Upload everything; return only successfully transferred files for builton."""
    output.info("")
    output.info("upload")
    sent = []
    for item in items:
        if not deploy_cmd.run(cfg, item.library, item.srcfile, [item.path], dry_run=dry_run):
            sent.append(item)
    return sent


def _build(
    cfg: Config, project: prj.Project, item: Item, dry_run: bool, verbose: bool, app: Any
) -> bool:
    output.info("")
    try:
        return run_cmd.run(cfg, item.task, str(item.path), dry_run, verbose, app,
                           project=project) == 0
    except (ConfigError, InvalidName) as exc:
        # Report this file failure and continue processing other items.
        output.fail(f"{item.path}  ->  {item.task}")
        output.note(str(exc))
        return False
