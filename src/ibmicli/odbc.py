"""SQL transport for db through IBM i Access ODBC.

pyodbc is imported lazily so missing ODBC components cannot break FTP commands.
The driver is installed separately; checks diagnoses each required component.
On Unix, use UTF-8 to avoid HY001 Memory allocation error with wide calls.
Windows uses the system ODBC manager and its default UTF-16 handling.

CONNTYPE=2 enforces read-only access on the connection, including rejecting
QCMDEXC. Only explicit --write selects CONNTYPE=0. NAM=1 and a leading comma
in DBQ put libraries in the job library list without selecting a default schema.
"""

from __future__ import annotations

import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .config import Config, ConfigError

# Registered driver names, including the Linux 64-bit variant
# and the iSeries compatibility name.
DRIVER = "IBM i Access ODBC Driver"
OLD_DRIVER = "iSeries Access ODBC Driver"

# Host server ports: mapper, database, signon.
PORTS = {449: "*SVRMAP", 8471: "*DATABASE", 8476: "*SIGNON"}

IBM_ACS_PAGE = "https://www.ibm.com/support/pages/ibm-i-access-client-solutions"
IBM_LINUX_REPO = "https://public.dhe.ibm.com/software/ibmi/products/odbc/"


class DbError(Exception):
    """Database or connection failure with cleaned driver text."""


@dataclass
class Check:
    """One component check, its result and any repair instructions."""

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


# --- Installation ----------------------------------------------------------


def _pyodbc() -> Any:
    """Load pyodbc or explain its missing system dependency."""
    try:
        import pyodbc
    except ImportError as exc:
        raise ConfigError(
            f"cannot load pyodbc: {exc}\n" + _fix_manager()
        ) from None
    return pyodbc


def _driver(pyodbc: Any) -> str:
    """Find the registered IBM driver or explain installation."""
    names = pyodbc.drivers()
    for wanted in (DRIVER, OLD_DRIVER):
        if wanted in names:
            return wanted
    for name in names:
        if DRIVER in name or OLD_DRIVER in name:
            return name
    found = ", ".join(names) if names else "none"
    raise ConfigError(f"driver '{DRIVER}' is not installed (ODBC drivers found: {found})\n" + _fix_driver())


def _fix_manager() -> str:
    """Platform-specific ODBC manager installation instructions."""
    # pyodbc is a CLI dependency but loads unixODBC dynamically.
    # Installing the manager resolves a missing shared library
    # without reinstalling the CLI.
    if sys.platform == "darwin":
        return _indent(
            "macOS requires unixODBC:  brew install unixodbc\n"
            "then check with:            ibmicli db --check"
        )
    if sys.platform.startswith("linux"):
        return _indent(
            "Linux requires unixODBC:  sudo apt install unixodbc   (or: sudo dnf install unixODBC)\n"
            "then check with:            ibmicli db --check"
        )
    return _indent("Reinstall ibmicli (see Installation and updates in the README).")


def _fix_driver() -> str:
    """Platform-specific IBM i Access driver installation instructions."""
    if sys.platform == "darwin":
        return _indent(
            "On macOS:\n"
            "  1. brew install unixodbc  (if not installed)\n"
            "  2. download 'IBM i Access Client Solutions - App Package for macOS' from\n"
            f"     {IBM_ACS_PAGE}  (requires an IBM ID)\n"
            "  3. install the .pkg to register the driver under /Library/IBMiAccess\n"
            "  4. check with: ibmicli db --check"
        )
    if sys.platform == "win32":
        return _indent(
            "On Windows:\n"
            "  1. download 'IBM i Access Client Solutions - Windows Application Package' from\n"
            f"     {IBM_ACS_PAGE}  (requires an IBM ID)\n"
            "  2. install it (the 64-bit ODBC driver is sufficient)\n"
            "  3. check with: ibmicli db --check"
        )
    return _indent(
        "On Linux use the IBM repository (ibm-iaccess package):\n"
        f"  {IBM_LINUX_REPO}\n"
        "  1. add the repository (.list for apt, .repo for dnf, from the page above)\n"
        "  2. sudo apt install unixodbc ibm-iaccess   (or: sudo dnf install unixODBC ibm-iaccess)\n"
        "  3. check with: ibmicli db --check"
    )


def _indent(text: str) -> str:
    return "\n".join("       " + line for line in text.splitlines())


# --- Connection ------------------------------------------------------------


def connection_string(
    cfg: Config,
    driver: str,
    libraries: list[str],
    password: str,
    write: bool = False,
    tx: bool = False,
) -> str:
    parts = {
        "DRIVER": "{" + driver + "}",
        "SYSTEM": cfg.host,
        "UID": cfg.user,
        "PWD": password,
        "NAM": "1",  # System naming resolves unqualified tables through the library list.
        # Without --tx every statement commits immediately.
        # With --tx use *CS; tables must be journaled.
        "CMT": "1" if tx else "0",
        # 2 = read-only; 0 = read/write, only with --write.
        "CONNTYPE": "0" if write else "2",
    }
    if libraries:
        parts["DBQ"] = "," + ",".join(libraries)
    # Brace quoting preserves semicolons and braces inside passwords.
    if any(c in password for c in ";{}"):
        parts["PWD"] = "{" + password.replace("}", "}}") + "}"
    return ";".join(f"{key}={value}" for key, value in parts.items())


def describe(cfg: Config, libraries: list[str], write: bool = False, tx: bool = False) -> str:
    """Connection description with the password masked."""
    return connection_string(cfg, DRIVER, libraries, "***", write, tx)


def connect(cfg: Config, libraries: list[str], write: bool = False, tx: bool = False) -> Any:
    """Open a connection; ConfigError for missing components, DbError for rejection."""
    pyodbc = _pyodbc()
    driver = _driver(pyodbc)
    try:
        conn = pyodbc.connect(
            connection_string(cfg, driver, libraries, cfg.password, write, tx),
            # Transactions use explicit commit/rollback, without autocommit.
            autocommit=not tx,
            timeout=10,
        )
    except pyodbc.Error as exc:
        raise DbError(_connection_problem(cfg, exc)) from None
    if sys.platform != "win32":
        _unix_encoding(pyodbc, conn)
    return conn


def _unix_encoding(pyodbc: Any, conn: Any) -> None:
    """Use UTF-8 both ways; Unix wide calls can fail with HY001 in UTF-16."""
    conn.setencoding(encoding="utf-8")
    conn.setdecoding(pyodbc.SQL_CHAR, encoding="utf-8")
    conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-8")


def _connection_problem(cfg: Config, exc: Exception) -> str:
    """Driver error plus any unreachable host server ports."""
    text = message(exc)
    # Probe ports concurrently to avoid three sequential timeouts
    # when the system is unreachable.
    with ThreadPoolExecutor(len(PORTS)) as pool:
        reachable = dict(zip(PORTS, pool.map(lambda port: _open(cfg.host, port), PORTS)))
    closed = [f"{port} ({server})" for port, server in PORTS.items() if not reachable[port]]
    if closed:
        text += (
            f"\nunreachable ports on {cfg.host}: {', '.join(closed)}"
            "\n" "on IBM i: STRHOSTSVR SERVER(*SVRMAP *DATABASE *SIGNON)"
            " (or check the firewall)"
        )
    return text


def _open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def message(exc: Exception) -> str:
    """Clean pyodbc prefixes and call metadata while preserving the SQL message."""
    args = getattr(exc, "args", ())
    text = str(args[1] if len(args) > 1 else exc)
    text = re.sub(r"^(\[[^\]]*\]\s*)+", "", text)
    text = re.sub(r"(\s*\((-?\d+|SQL\w+)\))+\s*$", "", text)
    return text.strip()


# --- Checks ----------------------------------------------------------------


def checks(cfg: Config) -> list[Check]:
    """Check required components in order, stopping at the first missing one."""
    found: list[Check] = []

    try:
        pyodbc = _pyodbc()
    except ConfigError as exc:
        return [Check("pyodbc module", False, *_split(str(exc)))]
    found.append(Check("pyodbc module", True, pyodbc.version))

    try:
        driver = _driver(pyodbc)
    except ConfigError as exc:
        return found + [Check("IBM i Access driver", False, *_split(str(exc)))]
    found.append(Check("IBM i Access driver", True, driver))

    try:
        conn = connect(cfg, [])
        try:
            cursor = conn.cursor()
            cursor.execute("select current server from sysibm.sysdummy1")
            server = str(cursor.fetchone()[0]).strip()
        finally:
            conn.close()
    except DbError as exc:
        return found + [
            Check(
                f"connection to {cfg.host}",
                False,
                str(exc),
                _indent("check host, user and password with: ibmicli set-conn"),
            )
        ]
    except Exception as exc:  # pyodbc.Error from the probe query.
        return found + [Check(f"connection to {cfg.host}", False, message(exc))]
    found.append(Check(f"connection to {cfg.host}", True, f"database {server}, read-only"))
    return found


def _split(text: str) -> tuple[str, str]:
    """ConfigError first line describes the problem; remaining lines explain a fix."""
    first, _, rest = text.partition("\n")
    return first, rest
