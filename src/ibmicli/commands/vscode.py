"""Install, inspect or remove the bundled VS Code extension.

The VSIX is built from package sources without npm. Reinstall after CLI
updates or tasks_key changes; tasks are queried dynamically.
"""

from __future__ import annotations

from .. import output, vscode as vscode_mod
from ..vscode import VSCodeError

ACTIONS = ("install", "uninstall", "status")


def run(action: str, tasks_key: str, extra: bool = False, only_ours: bool = False) -> int:
    choice = action.lower()
    if choice not in ACTIONS:
        output.error(f"vscode accepts {', '.join(ACTIONS)}")
        return 2

    try:
        if choice == "install":
            return vscode_mod.install(tasks_key, extra=extra, only_ours=only_ours)
        if choice == "uninstall":
            return vscode_mod.uninstall()
        return vscode_mod.status(tasks_key)
    except VSCodeError as exc:
        output.error(str(exc))
        return 2
