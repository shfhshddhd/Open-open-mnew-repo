"""Report duplicate command words across imported plugins and core handlers."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN_GLOB = ROOT / "telegram_userbot" / "plugins"
CORE_FILES = [
    ROOT / "telegram_userbot" / "userbot" / "client.py",
    *sorted((ROOT / "telegram_userbot" / "bot").glob("*.py")),
]
COMMAND_RE = re.compile(r"\\\.([A-Za-z0-9_]+)")


def commands_from_file(path: Path) -> list[str]:
    commands: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return commands
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "NewMessage"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "pattern" or not isinstance(keyword.value, ast.Constant):
                continue
            value = keyword.value.value
            if isinstance(value, str):
                match = COMMAND_RE.search(value)
                if match:
                    commands.append(match.group(1).lower())
    return commands


def main() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    files = [
        path
        for path in sorted(PLUGIN_GLOB.glob("*.py"))
        if path.name not in {"__init__.py", "bot.py", "help.py"}
    ] + [
        path for path in CORE_FILES if path.exists()
    ]
    for path in files:
        for command in commands_from_file(path):
            owners[command].append(str(path.relative_to(ROOT)))

    conflicts = {command: paths for command, paths in owners.items() if len(paths) > 1}
    print(f"Total command words: {len(owners)}")
    if not conflicts:
        print("No duplicate command words detected.")
        return
    print("Duplicate command words:")
    for command, paths in sorted(conflicts.items()):
        print(f"  .{command}: {', '.join(paths)}")


if __name__ == "__main__":
    main()