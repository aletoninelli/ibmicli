"""Run CL commands and report results.

setup() uses RCMD in the FTP job: library-list changes persist, but only
success or failure is returned. run() uses a QShell child job and returns
messages and status. Child jobs inherit the library list, but have separate
QTEMP libraries and do not inherit file overrides. State changes in a child
job do not affect later run() calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import evfevent, output
from . import session as sessions
from .config import Config
from .project import Project
from .session import Session, SessionError

# Message IDs identify the lines that belong in the job log.
# Compiler listings can contain thousands of other lines;
# only verbose mode displays them all.
MESSAGE_LINE = re.compile(r"^\s*[A-Z]{2,4}\d{4}[A-Z]?\b")

# Short output is treated entirely as messages.
LISTING_THRESHOLD = 40

# Routine job messages are hidden unless -v is supplied.
# These IDs describe known routine operations;
# they are not matched by text similarity.
# CPF4011: SQL precompiler buffer exceeds member record length,
# one harmless message for each source or copy member.
# CPC7301: QSQLTEMP1 expanded source created in QTEMP.
# CPC7305: QSQLTEMP1 member created.
# CPD4090: unavailable print device falls back to QPRINT.
# CPC0904: RETURNCODE data area created in QTEMP.
ROUTINE_MESSAGES = frozenset({"CPF4011", "CPC7301", "CPC7305", "CPD4090", "CPC0904"})

# At this severity or higher, compilation does not generate an object.
ERROR_SEVERITY = 20

SEVERITY_NAMES = {20: "error", 30: "severe error", 40: "severe error", 50: "severe error"}


@dataclass
class Result:
    """CL command result."""

    command: str
    rc: int
    lines: list[str] = field(default_factory=list)
    # Errors from a newly written event member; None when absent
    # or unchanged from the previous compilation.
    events: list[evfevent.Error] | None = None

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def messages(self) -> list[str]:
        """Message lines only when output contains a compiler listing."""
        if len(self.lines) <= LISTING_THRESHOLD:
            lines = self.lines
        else:
            lines = [line for line in self.lines if MESSAGE_LINE.match(line)]
        return [line.rstrip() for line in lines if _message_id(line) not in ROUTINE_MESSAGES]

    @property
    def listing_size(self) -> int:
        """Number of listing lines other than messages."""
        if len(self.lines) <= LISTING_THRESHOLD:
            return 0
        return sum(1 for line in self.lines if not MESSAGE_LINE.match(line))


def _message_id(line: str) -> str:
    match = MESSAGE_LINE.match(line)
    return match.group(0).strip() if match else ""


class Runner:
    """Execute CL and report results on the terminal."""

    def __init__(
        self,
        cfg: Config,
        dry_run: bool = False,
        verbose: bool = False,
        source: Path | None = None,
        events: tuple[str, str] | None = None,
        project: Project | None = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        # Local source used to locate compiler diagnostics, including copies.
        # Without it, report the remote member location.
        self.source = source
        # Explicit project used to locate local copies.
        self.project = project
        # Object library and member for LIBRARY/EVFEVENT(OBJECT).
        # Compilation with OPTION(*EVENTF) writes the diagnostics here.
        # The task knows these values from its project and source.
        self.events = events
        self._session: Session | None = None

    # --- Lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Runner":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._session is not None:
            sessions.release(self._session)
            self._session = None

    @property
    def session(self) -> Session:
        """Open lazily on the first command; never connect during dry-run."""
        if self._session is None:
            self._session = sessions.acquire(self.cfg)
        return self._session

    # --- Execution ---------------------------------------------------------

    def setup(self, command: str) -> bool:
        """Run an RCMD setup command whose job state must persist."""
        output.command(command)
        if self.dry_run:
            return True
        try:
            self.session.rcmd(command)
        except SessionError as exc:
            output.message(str(exc))
            return False
        return True

    def run(self, command: str) -> Result:
        """Run a CL command and report its messages."""
        output.command(command)
        if self.dry_run:
            return Result(command, 0)

        try:
            before = self._events_stamp()
            rc, lines = self.session.run_cl(command)
        except SessionError as exc:
            # Connection failures also become results so callers can
            # check ok without separate exception handling.
            output.message(str(exc))
            return Result(command, -1)

        result = Result(command, rc, lines)
        if not result.ok:
            result.events = self._read_events(before)
        self._report(result)
        return result

    def _events_member(self) -> list[str] | None:
        """Object event member, if available."""
        if self.events is None:
            return None
        library, name = self.events
        try:
            return self.session.get(library, evfevent.FILE, name)
        except SessionError:
            return None

    def _events_stamp(self) -> str | None:
        """Event timestamp before execution; unchanged means stale diagnostics."""
        lines = self._events_member()
        return evfevent.stamp(lines) if lines is not None else None

    def _read_events(self, before: str | None) -> list[evfevent.Error] | None:
        lines = self._events_member()
        if lines is None or evfevent.stamp(lines) == before:
            return None
        return evfevent.parse(lines, ERROR_SEVERITY)

    def run_all(self, *commands: str) -> bool:
        """Execute in order, stopping at the first failed command."""
        for command in commands:
            if not self.run(command).ok:
                return False
        return True

    # --- Output ------------------------------------------------------------

    def _report(self, result: Result) -> None:
        if self.verbose:
            for line in result.lines:
                output.message(line.rstrip())
        else:
            # Diagnostics with event locations are shown beside their source lines.
            # Do not duplicate their message IDs in the general response.
            placed = {error.msgid for error in result.events or [] if error.member}
            for line in result.messages:
                if _message_id(line) not in placed:
                    output.message(line)
            if result.events:
                for group in _by_place(result.events):
                    self._diagnostic(group)
            elif not result.ok and result.listing_size and self.events is not None:
                library, name = self.events
                output.message(
                    f"[no new diagnostics in {library}/{evfevent.FILE}({name}): "
                    "compile with OPTION(*EVENTF) to obtain source locations]"
                )
            if result.listing_size:
                output.message(
                    f"[{result.listing_size} compiler listing lines: use -v to display them]"
                )
        if not result.ok:
            output.message(f"[exit status {result.rc}]")

    def _diagnostic(self, group: list[evfevent.Error]) -> None:
        """Report a clickable source location, messages and an underlined source line."""
        first = group[0]
        path = (evfevent.local_file(self.cfg, first, self.source, self.project)
                if self.source else None)
        rows = _read(path) if path is not None else []
        if not first.member:
            where = "compiler did not report a source line"
        elif path is not None:
            where = f"{_shown(path)}:{first.line}" + (f":{first.column}" if first.column else "")
        else:
            # When no local copy exists, report the IBM i member location.
            where = f"{first.member}:{first.line}" + (f":{first.column}" if first.column else "")

        output.message("")
        output.problem(where)
        # Report each distinct diagnostic at the same location once.
        said = dict.fromkeys((one.msgid, one.severity, one.text) for one in group)
        for msgid, severity, text in said:
            name = SEVERITY_NAMES.get(severity, "severe error")
            output.message(f"  {msgid} {name} ({severity})  {text}")
        if 0 < first.line <= len(rows):
            _source_line(str(first.line), rows[first.line - 1], first.column, first.end_column)


def _by_place(errors: list[evfevent.Error]) -> list[list[evfevent.Error]]:
    """Group errors by source location in input order.

    Several messages can describe one faulty statement. Print its source
    line once; group messages without a source location at the end.
    """
    groups: dict[tuple[str, int], list[evfevent.Error]] = {}
    for error in sorted(errors, key=lambda error: not error.member):
        groups.setdefault((error.member, error.line), []).append(error)
    return list(groups.values())


def _source_line(number: str, text: str, column: int, end_column: int = 0) -> None:
    """Source line with known diagnostic columns underlined."""
    indent = len(text) - len(text.lstrip())
    output.message(f"  {number:>6} | {text.strip()}")
    if column > indent:
        width = end_column - column + 1 if end_column >= column else 1
        output.message(f"  {'':>6} | {' ' * (column - 1 - indent)}{'^' * width}")


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _shown(path: Path) -> str:
    """Path relative to the current folder when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)
