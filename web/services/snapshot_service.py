"""Live snapshot statistics service."""

from __future__ import annotations

from pathlib import Path


def get_snapshot_stats(snapshot_dir: str = "./live_snapshots") -> dict:
    """Return statistics about the live snapshot directory."""
    root = Path(snapshot_dir)
    stats = {
        "dir": str(root),
        "exists": root.exists(),
        "days": 0,
        "files": 0,
        "total_size_mb": 0,
        "stages": {},
        "latest_date": None,
    }

    if not root.exists():
        return stats

    dates = sorted([d for d in root.iterdir() if d.is_dir()], reverse=True)
    stats["days"] = len(dates)

    if dates:
        stats["latest_date"] = dates[0].name

    total_size = 0
    stage_files: dict[str, int] = {}

    for date_dir in dates:
        for stage_dir in date_dir.iterdir():
            if stage_dir.is_dir():
                stage_name = stage_dir.name
                n = len(list(stage_dir.glob("*.jsonl")))
                stage_files[stage_name] = stage_files.get(stage_name, 0) + n
                for f in stage_dir.glob("*.jsonl"):
                    total_size += f.stat().st_size

    stats["files"] = sum(stage_files.values())
    stats["total_size_mb"] = round(total_size / (1024 * 1024), 2)
    stats["stages"] = stage_files

    return stats
