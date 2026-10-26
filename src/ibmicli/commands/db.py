"""Run SQL through ODBC, read-only unless --write is explicit.

-from prepares standalone SQL from a source or editor buffer. JSON emits one
record per statement, including an error record for a failed statement.
Read all returned rows; query text controls any row limit.

Without --tx, writes commit immediately. With --write --tx, only the prompt
can commit. Every exit path rolls back uncommitted work, including a single
noninteractive statement and SIGHUP/SIGTERM.
"""

from __future__ import annotations

import csv
import itertools
import json
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, TextIO

from .. import config as cfgmod
from .. import odbc, output, project as prj, sqlsource
from ..config import Config
from ..naming import check_object

EXIT_WORDS = {"exit", "quit", "q", "\\q"}


@dataclass
class Target:
    """Result destination and format, plus transaction state."""

    fmt: str = "table"  # table, json, csv
    path: Path | None = None
    write: bool = False
    # Number of modifying statements waiting for commit or rollback.
    tx: bool = False
    pending: int = 0
    interactive: bool = False  # Only the interactive SQL prompt can commit.


def run(
    cfg: Config,
    sql: str | None,
    libraries: str | None = None,
    as_json: bool = False,
    dry_run: bool = False,
    as_csv: bool = False,
    out: str | None = None,
    write: bool = False,
    tx: bool = False,
    source: Path | None = None,
    line: int | None = None,
    stdin_text: bool = False,
    variables: list[str] | None = None,
    project: prj.Project | None = None,
) -> int:
    if tx and not write:
        output.error("--tx requires --write: ibmicli db --write --tx")
        return 2
    if as_json and as_csv:
        output.error("--json and --csv cannot be combined")
        return 2
    if source is None and (line is not None or stdin_text or variables):
        output.error("-line, --stdin and --var require -from FILE")
        return 2
    target = _target(as_json, as_csv, out)
    if target is None:
        return 2
    target.write = write
    target.tx = tx
    target.interactive = sql is None and source is None
    libs, origin = _libraries(cfg, project, libraries, source)

    # -from uses source syntax for the requested -line statement
    # or supplied text, including RPG exec sql and // comments.
    statements: list[str] | None = None
    lines: tuple[int, int] | None = None
    if source is not None:
        found = _from_source(cfg, source, sql, line, stdin_text)
        if found is None:
            return 2
        statements, lines = found
        if not statements:
            output.error("no SQL statement")
            return 2
        if dry_run and as_json:
            # VS Code plan: statements and host-variable names to request
            # before execution.
            plan: dict[str, Any] = {
                "statements": [
                    {"sql": text, "vars": sqlsource.host_variables(text)} for text in statements
                ],
                "libraries": libs,
            }
            if lines:
                plan["start"], plan["end"] = lines
            print(json.dumps(plan, ensure_ascii=False))
            return 0
        values = _variables(variables or [])
        if values is None:
            return 2
        # Dry-run substitutes supplied values and reports missing variables;
        # execution refuses any missing value before running SQL.
        statements = [
            sqlsource.substitute(text, values)
            if not dry_run or set(sqlsource.host_variables(text)) <= set(values)
            else text
            for text in statements
        ]
    else:
        if sql is None and not sys.stdin.isatty():
            sql = sys.stdin.read()
        if sql is not None:
            sql = _clean(sql)
            if not sql:
                output.error("no SQL statement")
                return 2
            # Keep directly supplied SQL as one statement: a CREATE
            # PROCEDURE body can contain its own semicolons.
            statements = [sql]

    if dry_run:
        output.dry(f"connection: {odbc.describe(cfg, libs, write, tx)}")
        for text in statements or []:
            output.dry(text)
            wanted = sqlsource.host_variables(text) if source is not None else []
            if wanted:
                sys.stdout.flush()  # Keep stdout DRY lines before stderr details.
                output.note("supply with --var: " + ", ".join(f":{name}" for name in wanted))
        if target.path:
            output.dry(f"result in {target.path} ({target.fmt})")
        return 0

    try:
        conn = odbc.connect(cfg, libs, write, tx)  # ConfigError for missing driver components is handled by cli.Commands.
    except odbc.DbError as exc:
        output.fail(f"connection to {cfg.host} failed")
        output.note(str(exc))
        return 1
    holder = [conn]  # The prompt can replace an expired connection.
    previous = _on_hangup() if tx else None
    try:
        if statements is not None:
            rc = 0
            for text in statements:
                rc = max(rc, _execute(conn, text, target))
            if target.pending:
                _end_tx(conn, target)
                output.note("with --tx, commit is available only in the prompt: ibmicli db --write --tx")
            return rc
        return _prompt(cfg, holder, libs, origin, target)
    finally:
        if tx:
            _end_tx(holder[0], target)
            _restore_hangup(previous)
        holder[0].close()


def _on_hangup() -> Any:
    """Exit through finally on terminal hangup or process termination.

    The database rolls back on disconnect too, but an orderly exit lets the
    CLI explicitly roll back and report the result.
    """
    def stop(*_args: object) -> None:
        raise SystemExit(1)

    previous = {}
    for name in ("SIGHUP", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:  # Windows has no SIGHUP.
            previous[number] = signal.signal(number, stop)
    return previous


def _restore_hangup(previous: Any) -> None:
    for number, handler in (previous or {}).items():
        signal.signal(number, handler)


def _end_tx(conn: Any, target: Target) -> None:
    """Roll back all uncommitted work on exit."""
    pending, target.pending = target.pending, 0
    try:
        conn.rollback()
    except Exception:
        return  # Already disconnected: IBM i rolled back the transaction.
    if pending:
        try:
            sys.stdout.flush()
            text = "uncommitted, rolled back" if pending == 1 else "uncommitted, rolled back"
            output.skip(f"rollback: {_statements(pending)} {text}")
        except OSError:
            pass  # The terminal is closed, so reporting may fail.


def _statements(count: int) -> str:
    return "1 statement" if count == 1 else f"{count} statements"


def _target(as_json: bool, as_csv: bool, out: str | None) -> Target | None:
    """Result format from --json/--csv, otherwise the --out extension."""
    fmt = "json" if as_json else "csv" if as_csv else ""
    path = Path(out).expanduser() if out else None
    if path is not None:
        if path.is_dir():
            output.error(f"--out: {path} is a directory; supply a filename")
            return None
        if not path.parent.is_dir():
            output.error(f"--out: directory {path.parent} does not exist")
            return None
        fmt = fmt or {".json": "json", ".csv": "csv"}.get(path.suffix.lower(), "")
    return Target(fmt or "table", path)


def check(cfg: Config | None) -> int:
    """Check db dependencies and explain repairs."""
    found = odbc.checks(cfg) if cfg is not None else _checks_without_config()
    for item in found:
        sys.stdout.flush()  # Preserve stdout success and stderr failure order.
        if item.ok:
            output.ok(f"{item.name}: {item.detail}")
        else:
            output.fail(f"{item.name}: {item.detail}")
            if item.fix:
                output.note(item.fix)
    if cfg is None:
        sys.stdout.flush()
        output.fail("connection: missing configuration")
        output.note("create it with: ibmicli set-conn")
        return 1
    return 0 if all(item.ok for item in found) else 1


def _checks_without_config() -> list[odbc.Check]:
    """Check the driver and Python module before connection configuration exists."""
    dummy = Config(host="", user="", password="", path=cfgmod.config_path())
    return [item for item in odbc.checks(dummy) if not item.name.startswith("connection")]


def _libraries(
    cfg: Config,
    project: prj.Project | None,
    libraries: str | None,
    source: Path | None = None,
) -> tuple[list[str], str]:
    """Libraries in environment search order and their origin for the prompt.

    Exclude QTEMP, which is empty on a fresh connection. With -proj, use the
    source or current folder app's environment; unknown app environments
    raise ConfigError. Outside apps, use the active environment.
    """
    if libraries:
        names = libraries.replace(",", " ").split()
        return [check_object(name, "library") for name in names], "-lib"
    start = source if source is not None else Path.cwd()
    app = project.app_of(start) if project is not None else None
    env = cfg.named_env(app.env) if app is not None and app.env else cfg.env()
    names = [name for name in (env.search if env is not None else ()) if name != "QTEMP"]
    objlib = prj.object_library(app, env)
    if objlib and objlib not in names:
        names.insert(0, objlib)
    if not names:
        return [], ""
    return names, f"env {env.name}" if env is not None else f"app {app.name}"


def _from_source(
    cfg: Config, source: Path, sql: str | None, line: int | None, stdin_text: bool
) -> tuple[list[str], tuple[int, int] | None] | None:
    """Prepared -from statements and optional -line source range; None for invalid input."""
    rpg = sqlsource.is_rpg(source)
    if line is not None:
        if sql is not None:
            output.error("-line and SQL text cannot be combined: choose one")
            return None
        if stdin_text:
            # Use editor-buffer line numbers, including unsaved changes.
            text = sys.stdin.read()
        else:
            try:
                text = source.read_text(encoding=cfg.local_charset, errors="replace")
            except OSError as exc:
                output.error(f"{source}: {exc.strerror or exc}")
                return None
        piece = sqlsource.at_line(text, line, rpg)
        return [sqlsource.prepare(piece.text)], (piece.start, piece.end)
    if stdin_text:
        output.error("--stdin requires -line: text comes from the editor")
        return None
    if sql is None:
        output.error("with -from, supply SQL text or -line N")
        return None
    return [sqlsource.prepare(piece.text) for piece in sqlsource.pieces(sql, rpg)], None


def _variables(raw: list[str]) -> dict[str, str] | None:
    """Parse --var NAME=VALUE; values are SQL text, including quotes."""
    values: dict[str, str] = {}
    for item in raw:
        name, equal, value = item.partition("=")
        name = name.strip().lstrip(":").strip().upper()
        if not equal or not name or not value.strip():
            output.error(f"--var {item}: use NAME=VALUE, e.g. --var CODE=\"'A01'\"")
            return None
        values[name] = value.strip()
    return values


def _clean(sql: str) -> str:
    """Remove outer whitespace and trailing semicolons for one driver statement."""
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


# --- Execution -------------------------------------------------------------


def _execute(conn: Any, sql: str, target: Target) -> int:
    cursor = conn.cursor()
    try:
        try:
            cursor.execute(sql)
            if cursor.description is None:
                _done(cursor, target.fmt == "json", sql)
                if target.tx:
                    target.pending += 1
                    if target.interactive:
                        sys.stdout.flush()
                        output.note(
                            f"pending ({_statements(target.pending)}): "
                            "'commit' to confirm, 'rollback' to discard"
                        )
                return 0
            columns = [str(d[0]) for d in cursor.description]
            rows = cursor.fetchall()
        except Exception as exc:  # pyodbc.Error, such as invalid SQL or a missing table.
            text = odbc.message(exc)
            notes = []
            if "read only" in text.lower():
                notes.append("db is read-only: to allow writes, use ibmicli db --write")
            if "SQL7008" in text:
                notes.append(
                    "the table is not journaled, so commit/rollback is unavailable: "
                    "use --write without --tx, or journal the table (STRJRNPF)"
                )
            output.fail(text)
            for note in notes:
                output.note(note)
            if target.fmt == "json" and target.path is None:
                # Errors also produce one JSON record per statement,
                # including its SQL text, so callers can identify which
                # statement failed without losing execution order.
                print(json.dumps({"sql": sql, "error": text, "notes": notes}, ensure_ascii=False))
            return 1
    finally:
        cursor.close()

    if target.path is None:
        _render(target.fmt, columns, rows, sys.stdout, sql)
        if target.fmt == "table":
            _footer(len(rows))  # Row counts go to stderr to keep data output clean.
        return 0

    # Write the destination only after a successful query;
    # SQL errors leave the previous result intact.
    try:
        with open(target.path, "w", encoding="utf-8", newline="") as stream:
            _render(target.fmt, columns, rows, stream, sql)
    except OSError as exc:
        output.fail(f"{target.path}: {exc.strerror or exc}")
        return 1
    count = "1 row" if len(rows) == 1 else f"{len(rows)} rows"
    output.ok(f"{count} in {target.path}")
    return 0


def _render(fmt: str, columns: list[str], rows: list[Any], stream: TextIO, sql: str = "") -> None:
    if fmt == "json":
        _json(columns, rows, stream, sql)
    elif fmt == "csv":
        _csv(columns, rows, stream)
    else:
        _table(columns, rows, stream)


def _done(cursor: Any, as_json: bool, sql: str = "") -> None:
    """Statement without a result set, such as SET or VALUES INTO."""
    count = cursor.rowcount
    if as_json:
        print(json.dumps({"sql": sql, "columns": [], "rows": [], "rowcount": count}, ensure_ascii=False))
    else:
        rows = "1 row" if count == 1 else f"{count} rows"
        output.ok("executed" + (f", {rows}" if count and count > 0 else ""))


def _footer(count: int) -> None:
    text = "1 row" if count == 1 else f"{count} rows"
    sys.stdout.flush()  # Keep table output before its row count, also when redirected.
    output.note(text)


# --- Formats ---------------------------------------------------------------


def _json(columns: list[str], rows: list[Any], stream: TextIO, sql: str = "") -> None:
    """One JSON record with SQL and row objects, using unique column names."""
    names = _unique(columns)
    data = [{name: _json_value(value) for name, value in zip(names, row)} for row in rows]
    print(json.dumps({"sql": sql, "columns": names, "rows": data}, ensure_ascii=False), file=stream)


def _csv(columns: list[str], rows: list[Any], stream: TextIO) -> None:
    """CSV with a header; NULL becomes an empty field."""
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if value is None else _text(value) for value in row])


def _unique(columns: list[str]) -> list[str]:
    """Duplicate join column names receive suffixes such as NAME_2."""
    seen: dict[str, int] = {}
    names = []
    for name in columns:
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return names


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value.rstrip()  # CHAR values arrive padded with blanks.
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        number = float(value)
        # Use a string when float conversion would lose decimal precision.
        return number if Decimal(repr(number)) == value else str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex().upper()
    return str(value)  # date, time, timestamp


def _text(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, (bytes, bytearray)):
        return value.hex().upper()
    return str(value)  # str preserves Decimal scale, e.g. 12.50.


def _table(columns: list[str], rows: list[Any], stream: TextIO) -> None:
    cells = [[_text(value) for value in row] for row in rows]
    widths = [
        max([len(name)] + [len(row[i]) for row in cells]) for i, name in enumerate(columns)
    ]
    # Right-align numeric columns, as in 5250.
    numeric = [
        bool(rows)
        and all(
            row[i] is None or (isinstance(row[i], (int, float, Decimal)) and not isinstance(row[i], bool))
            for row in rows
        )
        for i in range(len(columns))
    ]

    def line(values: list[str]) -> str:
        parts = [
            value.rjust(width) if right else value.ljust(width)
            for value, width, right in zip(values, widths, numeric)
        ]
        return "  ".join(parts).rstrip()

    print(line(columns), file=stream)
    print("  ".join("-" * width for width in widths), file=stream)
    for row in cells:
        print(line(row), file=stream)


# --- Prompt ----------------------------------------------------------------


def _prompt(
    cfg: Config,
    holder: list[Any],
    libs: list[str],
    origin: str,
    target: Target,
    read: Callable[[], str] | None = None,
) -> int:
    """Read blocks on one connection until exit.

    read returns a possibly multiline block with semicolon-separated
    statements. EOFError exits; KeyboardInterrupt cancels input. Tests can
    supply a reader instead of the default prompt editor.
    """
    where = f"; libraries {' '.join(libs)} ({origin})" if libs else ""
    mode = "READ/WRITE" if target.write else "read-only"
    output.info(f"SQL on {cfg.host} as {cfg.user}, {mode}{where}")
    if target.path:
        output.info(f"Results in {target.path} ({target.fmt}), overwritten after each successful query.")
    if target.tx:
        output.info("TRANSACTION: changes remain pending until 'commit'; 'rollback' discards them.")
        output.info("Exiting in any way rolls back uncommitted work.")

    if read is None:
        read = _editor(target, _tables(holder[0], libs))

    while True:
        try:
            text = read()
        except EOFError:  # Ctrl-D
            break
        except KeyboardInterrupt:  # Ctrl-C cancels the current input block.
            continue
        if _clean(text).lower() in EXIT_WORDS:
            break
        for sql in split_statements(text):
            holder[0] = _execute_again(cfg, holder[0], libs, sql, target)
    return 0


def split_statements(text: str) -> list[str]:
    """Split unquoted, uncommented semicolons in one input block."""
    statements, current = [], []
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current.append(char)
        elif text.startswith("--", i):  # Comment through the end of the line.
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            current.append(text[i:end])
            i = end
            continue
        elif char == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    statements.append("".join(current))
    return [sql for sql in (_clean(part) for part in statements) if sql and not _only_comments(sql)]


def _only_comments(sql: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in sql.splitlines())


def _execute_again(cfg: Config, conn: Any, libs: list[str], sql: str, target: Target) -> Any:
    """Execute after reopening an expired connection when needed."""
    if target.tx and sql.lower() in ("commit", "rollback"):
        _commit_or_rollback(conn, sql.lower(), target)
        return conn
    if not _alive(conn):
        try:
            conn.close()
        except Exception:
            pass
        if target.pending:
            output.fail(
                f"connection lost: {_statements(target.pending)} pending "
                "rolled back by IBM i"
            )
            target.pending = 0
        try:
            conn = odbc.connect(cfg, libs, target.write, target.tx)
        except odbc.DbError as exc:
            output.fail(f"connection lost: {exc}")
            return conn
    _execute(conn, sql, target)
    return conn


def _commit_or_rollback(conn: Any, word: str, target: Target) -> None:
    pending = target.pending
    if word == "commit":
        done = "1 statement committed" if pending == 1 else f"{pending} statements committed"
    else:
        done = "1 statement rolled back" if pending == 1 else f"{pending} statements rolled back"
    try:
        if word == "commit":
            conn.commit()
        else:
            conn.rollback()
    except Exception as exc:
        output.fail(f"{word}: {odbc.message(exc)}")
        return
    target.pending = 0
    if word == "commit":
        output.ok(f"commit: {done}" if pending else "commit: nothing pending")
    else:
        output.ok(f"rollback: {done}" if pending else "rollback: nothing pending")


def _alive(conn: Any) -> bool:
    try:
        cursor = conn.cursor()
        cursor.execute("values 1")
        cursor.fetchall()
        cursor.close()
        return True
    except Exception:
        return False


# --- Editor ----------------------------------------------------------------
# prompt_toolkit edits a multiline statement as one block
# and recalls it intact from history, unlike single-line readline.
# This also works on Windows.

SQL_KEYWORDS = (
    "SELECT FROM WHERE AND OR NOT IN EXISTS BETWEEN LIKE IS NULL AS DISTINCT "
    "ORDER BY GROUP HAVING JOIN INNER LEFT RIGHT FULL OUTER CROSS ON UNION ALL "
    "EXCEPT INTERSECT WITH FETCH FIRST ROWS ONLY LIMIT OFFSET CASE WHEN THEN "
    "ELSE END CAST COUNT SUM MIN MAX AVG COALESCE TRIM SUBSTR UPPER LOWER "
    "CURRENT_DATE CURRENT_TIMESTAMP VALUES INSERT INTO UPDATE SET DELETE "
    "MERGE USING MATCHED CREATE TABLE VIEW INDEX DROP ALTER ADD COLUMN "
    "COMMIT ROLLBACK DESC ASC TABLE QSYS2 SYSIBM SYSDUMMY1"
).split()


def _tables(conn: Any, libs: list[str]) -> list[str]:
    """Best-effort table names for TAB completion."""
    if not libs:
        return []
    try:
        cursor = conn.cursor()
        marks = ", ".join("?" for _ in libs)
        cursor.execute(
            f"select distinct table_name from qsys2.systables where table_schema in ({marks})",
            *libs,
        )
        names = [str(row[0]).strip() for row in cursor.fetchall()]
        cursor.close()
        return names
    except Exception:
        return []


def ready(text: str, at_end: bool = True) -> bool:
    """Whether Enter executes: terminator, prompt command, or empty final line."""
    stripped = text.strip()
    if not stripped:
        return True  # Empty Enter starts a new prompt.
    if stripped.endswith(";"):
        return True
    if _clean(stripped).lower() in EXIT_WORDS | {"commit", "rollback"}:
        return True
    return at_end and text.endswith("\n")


# Bound SQL history size so the file
# does not grow indefinitely.
HISTORY_KEEP = 20


def _last_history(file_history: Any, path: Path, keep: int) -> Any:
    """FileHistory limited to the latest keep distinct statements.

    Preserve prompt_toolkit's block format while rewriting on each entry;
    FileHistory itself would only append.
    """

    class LastHistory(file_history):  # type: ignore[misc, valid-type]
        def load_history_strings(self) -> Any:  # Most recent first.
            return itertools.islice(super().load_history_strings(), keep)

        def store_string(self, string: str) -> None:
            # Reexecuted statements move to the top without duplication.
            older = list(
                itertools.islice(
                    (entry for entry in super().load_history_strings() if entry != string),
                    keep - 1,
                )
            )
            # Only the newest block needs a timestamp.
            blocks = [
                "".join(f"+{line}\n" for line in entry.split("\n"))
                for entry in [*reversed(older), string]
            ]
            text = "".join(f"\n#\n{block}" for block in blocks[:-1])
            text += f"\n# {datetime.now()}\n{blocks[-1]}"
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)

    return LastHistory(str(path))


def _editor(target: Target, tables: list[str]) -> Callable[[], str]:
    """Build the SQL editor and return its block reader."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.lexers import PygmentsLexer
    from pygments.lexers.sql import SqlLexer

    keys = KeyBindings()

    @keys.add("enter")
    def _enter(event: Any) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:  # Enter selects or closes an open TAB menu.
            if buffer.complete_state.current_completion:
                buffer.apply_completion(buffer.complete_state.current_completion)
            else:
                buffer.cancel_completion()
            return
        if ready(buffer.text, buffer.document.is_cursor_at_the_end):
            # The empty execution-trigger line is not part of the statement;
            # remove it so history has no extra trailing line.
            trailing = len(buffer.text) - len(buffer.text.rstrip("\n"))
            if trailing and buffer.document.is_cursor_at_the_end:
                buffer.delete_before_cursor(trailing)
            buffer.validate_and_handle()
        else:
            buffer.insert_text("\n")

    @keys.add("escape", "enter")
    def _run_now(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    history = cfgmod.config_path().parent / "sql_history"
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        stored: Any = _last_history(FileHistory, history, HISTORY_KEEP)
    except OSError:
        stored = None

    def toolbar() -> str:
        help_text = " Enter: new line   ; or blank line: run   Esc+Enter: run now   TAB: complete   Ctrl-D: exit"
        if target.tx:
            state = f"pending: {_statements(target.pending)}" if target.pending else "nothing pending"
            help_text += f"   | TRANSACTION, {state}"
        return help_text

    session: Any = PromptSession(
        multiline=True,
        key_bindings=keys,
        history=stored,
        lexer=PygmentsLexer(SqlLexer),
        completer=WordCompleter(SQL_KEYWORDS + tables, ignore_case=True),
        complete_while_typing=False,
        prompt_continuation=lambda width, *_: "...> ".rjust(width),
        bottom_toolbar=toolbar,
    )

    def read() -> str:
        return session.prompt(lambda: "sql*> " if target.pending else "sql> ")

    return read
