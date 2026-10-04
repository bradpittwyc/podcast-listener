"""Bound inactive episode caches while protecting jobs currently using their files."""
import re
import time
from pathlib import Path


def prune(folder, protected=(), max_bytes=2 * 1024**3, max_age=30 * 86400):
    protected = set(protected)
    groups = {}
    for path in Path(folder).iterdir():
        match = re.match(r'^([0-9a-f]{32})(?:[.-])', path.name)
        if not match or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        groups.setdefault(match[1], []).append((path, stat.st_size, stat.st_mtime))
    total = sum(size for files in groups.values() for _, size, _ in files)
    removed = 0
    for key, files in sorted(groups.items(), key=lambda item: max(file[2] for file in item[1])):
        if key in protected or (total <= max_bytes and max(file[2] for file in files) >= time.time() - max_age):
            continue
        for path, size, _ in files:
            try:
                path.unlink()
                removed += 1
                total -= size
            except OSError:
                pass
    return removed
