"""Local filenames and IBM i source member names. HELLO.sqlrpgle maps to HELLO."""

from __future__ import annotations

from pathlib import Path

MAX_MEMBER_LEN = 10


class InvalidName(Exception):
    """Invalid member or object name."""


def check_member(name: str) -> str:
    """Normalize a member name to uppercase and validate its length."""
    member = name.strip().upper()
    if not member:
        raise InvalidName("empty member name")
    if len(member) > MAX_MEMBER_LEN:
        raise InvalidName(f"member name '{member}' exceeds {MAX_MEMBER_LEN} characters")
    return member


def check_object(name: str, what: str) -> str:
    """Normalize and validate a library or source-file name."""
    obj = name.strip().upper()
    if not obj:
        raise InvalidName(f"{what} not supplied")
    if len(obj) > 10:
        raise InvalidName(f"{what} '{obj}' exceeds 10 characters")
    return obj


def split_file(path: Path) -> tuple[str, str]:
    """Local filename -> member name and extension without a dot."""
    stem, _, ext = path.name.rpartition(".")
    if not stem:  # Without a dot, rpartition puts the whole name in ext.
        stem, ext = path.name, ""
    return check_member(stem), ext


def parse_target(arg: str) -> tuple[str, str, str]:
    """Parse LIBRARY/FILE,MEMBER, as printed by list.

    Also accepts LIBRARY/FILE(MEMBER) and member names with a local extension.
    """
    text = arg.strip()
    if not text:
        raise InvalidName("supply a member as LIBRARY/FILE,MEMBER")

    if text.endswith(")") and "(" in text:
        head, _, member = text[:-1].partition("(")
    else:
        head, sep, member = text.partition(",")
        if not sep:
            raise InvalidName(f"'{arg}': missing member; use LIBRARY/FILE,MEMBER")

    library, sep, srcfile = head.partition("/")
    if not sep:
        raise InvalidName(f"'{arg}': missing source file; use LIBRARY/FILE,MEMBER")

    return (
        check_object(library, "library"),
        check_object(srcfile, "source file"),
        split_member_arg(member)[0],
    )


def split_member_arg(arg: str) -> tuple[str, str]:
    """-mbr argument -> member name and optional local extension."""
    return split_file(Path(arg.strip()))
