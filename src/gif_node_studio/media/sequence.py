from __future__ import annotations

import re
from pathlib import Path


def discover_numbered_sequence(selected: str | Path) -> tuple[Path, ...]:
    selected = Path(selected)
    match = re.match(r"^(.*?)(\d+)(\.[^.]+)$", selected.name)
    if not match:
        raise ValueError("filename must end with a numeric sequence")
    prefix, _number, suffix = match.groups()
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(suffix)}$")
    matches: list[tuple[int, Path]] = []
    for candidate in selected.parent.iterdir():
        found = pattern.match(candidate.name)
        if found:
            matches.append((int(found.group(1)), candidate))
    return tuple(path for _, path in sorted(matches))
