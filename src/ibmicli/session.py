"""Shared FTP transport to IBM i.

Transfers use ASCII mode: the server converts between EBCDIC and the remote
character set (ISO-8859-1 by default); local files use UTF-8.

Uploads use ftplib.storlines. curl 8.7.x corrupts FTP ASCII uploads at 64 KB
buffer boundaries during LF-to-CRLF conversion without reporting an error.
Sending one line at a time avoids this data loss.
"""

from __future__ import annotations

import ftplib
import os
from io import BytesIO
from pathlib import Path

from . import output
from .config import Config

# Sentinel carrying the command exit status at the end of QShell output.
_RC_MARKER = "ibmicli-rc="


class SessionError(Exception):
    """Connection, authentication or transfer failure."""


class Session:
    """Lazy FTP connection, closed at the end of a command.

    With keep_open, check for server idle timeouts and reconnect before an
    operation. Shared shell sessions survive individual commands.
    """

    def __init__(self, cfg: Config, keep_open: bool = False) -> None:
        self.cfg = cfg
        self.keep_open = keep_open
        self._ftp: ftplib.FTP | None = None
        self._cl_calls = 0  # Unique suffix for each remote output file.

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except Exception:
                pass
            self._ftp = None

    @property
    def ftp(self) -> ftplib.FTP:
        if self._ftp is not None and self.keep_open and not self._alive():
            self._ftp = None
        if self._ftp is None:
            self._ftp = self._connect()
        return self._ftp

    def _alive(self) -> bool:
        """Use NOOP to detect expired idle connections."""
        try:
            self._ftp.voidcmd("NOOP")  # type: ignore[union-attr]
            return True
        except Exception:
            try:
                self._ftp.close()  # type: ignore[union-attr]
            except Exception:
                pass
            return False

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP(timeout=120, encoding=self.cfg.remote_charset)
        try:
            ftp.connect(self.cfg.host)
            ftp.login(self.cfg.user, self.cfg.password)
            # NAMEFMT 1 uses IFS paths (/QSYS.LIB/LIB.LIB/FILE.FILE/MBR.MBR).
            ftp.sendcmd("SITE NAMEFMT 1")
            if self.cfg.ccsid:
                ftp.sendcmd(f"SITE CCSID {self.cfg.ccsid}")
        except ftplib.all_errors as exc:
            raise SessionError(f"{self.cfg.host}: {exc}") from exc
        return ftp

    # --- Member operations -------------------------------------------------

    @staticmethod
    def member_path(library: str, srcfile: str, member: str) -> str:
        return f"/QSYS.LIB/{library}.LIB/{srcfile}.FILE/{member}.MBR"

    def put(self, library: str, srcfile: str, member: str, data: bytes) -> None:
        remote = self.member_path(library, srcfile, member)
        try:
            # storlines terminates every line, including the last, with CRLF.
            # This prevents a truncated final member record.
            self.ftp.storlines(f"STOR {remote}", BytesIO(data))
        except ftplib.all_errors as exc:
            raise SessionError(str(exc)) from exc

    def get(self, library: str, srcfile: str, member: str) -> list[str]:
        remote = self.member_path(library, srcfile, member)
        lines: list[str] = []
        try:
            self.ftp.retrlines(f"RETR {remote}", lines.append)
        except ftplib.all_errors as exc:
            raise SessionError(str(exc)) from exc
        # Member records have fixed length; remove padding blanks.
        return [line.rstrip() for line in lines]

    def members(self, library: str, srcfile: str) -> list[str]:
        remote = f"/QSYS.LIB/{library}.LIB/{srcfile}.FILE"
        try:
            entries = self.ftp.nlst(remote)
        except ftplib.error_perm as exc:
            raise SessionError(str(exc)) from exc
        except ftplib.all_errors as exc:
            raise SessionError(str(exc)) from exc
        names = []
        for entry in entries:
            name = entry.rsplit("/", 1)[-1].strip()
            if name.upper().endswith(".MBR"):
                name = name[:-4]
            if name:
                names.append(name.upper())
        return sorted(names)

    def srcfiles(self, library: str) -> list[str]:
        """Names of *FILE objects in a library, including source files."""
        remote = f"/QSYS.LIB/{library}.LIB"
        try:
            entries = self.ftp.nlst(remote)
        except ftplib.all_errors as exc:
            raise SessionError(str(exc)) from exc
        names = set()
        for entry in entries:
            name = entry.rsplit("/", 1)[-1].strip()
            if name.upper().endswith(".FILE"):
                names.add(name[:-5].upper())
        return sorted(names)

    def rcmd(self, command: str) -> None:
        """Execute CL through RCMD; raise SessionError on failure."""
        try:
            self.ftp.sendcmd(f"RCMD {command}")
        except ftplib.all_errors as exc:
            raise SessionError(str(exc)) from exc

    def run_cl(self, command: str) -> tuple[int, list[str]]:
        """Execute CL and return its exit status and response lines.

        QShell redirects output to a remote IFS file, then reads it back.
        Each invocation is a child job: persistent library-list setup belongs
        in rcmd, which runs in the FTP job.
        """
        if '"' in command:
            raise SessionError("CL commands containing double quotes are not supported")
        # The system exit status is authoritative: RCMD returns 250 even
        # when CL inside QSH fails.
        return self.run_qsh(f'system "{command}"')

    def run_qsh(self, script: str) -> tuple[int, list[str]]:
        """Run QShell and read its remote output file, including the status sentinel."""
        self._cl_calls += 1
        log = f"/tmp/ibmicli-{os.getpid()}-{self._cl_calls}.log"
        full = f'{script} > {log} 2>&1; echo "{_RC_MARKER}$?" >> {log}'
        # QSH CMD('...') is a CL literal. Double quotes represented by single
        # apostrophes inside CL parameters (TEXT('...'), COMPILEOPT('...'))
        # must be escaped by doubling the apostrophe so the outer literal
        # does not terminate early.
        self.rcmd("QSH CMD('" + full.replace("'", "''") + "')")

        lines = self._read_ifs(log)
        self._remove_ifs(log)

        rc = -1
        messages = []
        for line in lines:
            if line.startswith(_RC_MARKER):
                rc = int(line[len(_RC_MARKER):].strip() or -1)
            elif line.strip():
                messages.append(line.rstrip())
        return rc, messages

    def library_list(self) -> list[tuple[str, str]]:
        """FTP job library list as (library, type), with SYS/PRD/CUR/USR types.

        QShell liblist inherits the FTP job list, including RCMD changes.
        """
        rc, lines = self.run_qsh("liblist")
        if rc != 0:
            raise SessionError("cannot read the library list: " + " ".join(lines[:3]))
        found = []
        for line in lines:
            parts = line.split()
            if len(parts) == 2 and parts[1] in ("SYS", "PRD", "CUR", "USR"):
                found.append((parts[0].upper(), parts[1]))
        return found

    def _read_ifs(self, path: str) -> list[str]:
        lines: list[str] = []
        try:
            self.ftp.retrlines(f"RETR {path}", lines.append)
        except ftplib.all_errors as exc:
            raise SessionError(f"cannot read command output ({path}): {exc}") from exc
        return lines

    def _remove_ifs(self, path: str) -> None:
        """Best-effort cleanup of the temporary remote file."""
        try:
            self.rcmd(f"RMVLNK OBJLNK('{path}')")
        except SessionError:
            pass


# --- Shared session --------------------------------------------------------
# Individual invocations own their connections. The shell and task runner
# pin a session to reuse one FTP job across commands, avoiding repeated
# logins and preserving setup state. acquire returns the pinned session;
# release leaves it open until the owner calls unpin.

_pinned: Session | None = None


def pin(cfg: Config) -> Session:
    """Pin a lazy shared session for subsequent commands."""
    global _pinned
    if _pinned is None:
        _pinned = Session(cfg, keep_open=True)
    return _pinned


def unpin() -> None:
    """Close the shared session and restore per-command connections."""
    global _pinned
    if _pinned is not None:
        _pinned.close()
        _pinned = None


def is_pinned() -> bool:
    """Whether a shell or task has pinned a shared session."""
    return _pinned is not None


def acquire(cfg: Config) -> Session:
    """Acquire the shared session, or create a command-owned session."""
    return _pinned if _pinned is not None else Session(cfg)


def release(session: Session) -> None:
    """Close a session only when the command owns it."""
    if session is not _pinned:
        session.close()


def read_local(path: Path, cfg: Config) -> bytes:
    """Local file -> member bytes in the server character set."""
    try:
        text = path.read_text(encoding=cfg.local_charset)
    except UnicodeDecodeError as exc:
        raise SessionError(f"not valid {cfg.local_charset}: {exc}") from exc
    try:
        return text.encode(cfg.remote_charset)
    except UnicodeEncodeError as exc:
        line = text.count("\n", 0, exc.start) + 1
        bad = text[exc.start : exc.end]
        output.note(
            f"character {bad!r} at line {line} cannot be encoded in "
            f"{cfg.remote_charset}; replaced with '?'"
        )
        return text.encode(cfg.remote_charset, errors="replace")
