"""Timestamped mine-run archives under ~/.claude-judge/runs/.

Each `mine.py` run writes a full snapshot to runs/YYYY-MM-DD_HHMMSS/.
`latest.json` + `latest` symlink point at the newest run. Live judge hooks
read published copies of rulebook.json from the store root.
"""
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
RUNS_DIR = ROOT / "runs"
LATEST_JSON = ROOT / "latest.json"
LATEST_LINK = ROOT / "latest"

# Copied to store root after each mine/label so hooks keep a stable path.
PUBLISHED_FILES = ("rulebook.json",)

MINE_ARTIFACTS = (
    "shortlist.json",
    "findings.json",
    "baseline.json",
    "rulebook.json",
    "findings_report.md",
    "findings_deep_dive.md",
    "change_first.md",
    "config_report.md",
    "dashboard_data.json",
    "guide.md",
    "run_meta.json",
)

DETECTOR_VERSION = 3


def new_run_dir(root=None):
    """Create runs/<timestamp>/ and return (run_id, path)."""
    root = root or ROOT
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    run_id = stamp
    run_dir = runs / run_id
    suffix = 1
    while run_dir.exists():
        run_id = f"{stamp}_{suffix}"
        run_dir = runs / run_id
        suffix += 1
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def finalize_run(run_id, run_dir, meta, root=None):
    """Record latest pointer and publish rulebook for live hooks."""
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "path": str(run_dir.relative_to(root)),
        **meta,
    }
    (root / "latest.json").write_text(json.dumps(record, indent=1))
    link = root / "latest"
    try:
        if link.is_symlink():
            link.unlink()
        elif link.exists() and not link.is_dir():
            link.unlink()
        link.symlink_to(run_dir.relative_to(root))
    except OSError:
        pass
    publish_artifacts(run_dir, root)
    return record


def publish_artifacts(run_dir, root=None):
    root = root or ROOT
    for name in PUBLISHED_FILES:
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, root / name)


def get_latest_run_dir(root=None):
    """Newest archived run, or store root for legacy flat layout."""
    root = root or ROOT
    latest_file = root / "latest.json"
    if latest_file.exists():
        try:
            rec = json.loads(latest_file.read_text())
            path = root / rec.get("path", "")
            if path.is_dir():
                return path
        except (json.JSONDecodeError, TypeError, OSError):
            pass
    link = root / "latest"
    if link.is_symlink():
        return link.resolve()
    if link.is_dir():
        return link
    return root


def resolve_run_dir(run_id=None, root=None):
    """Explicit run id, else latest, else legacy root."""
    root = root or ROOT
    if run_id:
        path = root / "runs" / run_id
        if path.is_dir():
            return path
        raise FileNotFoundError(f"run not found: {path}")
    return get_latest_run_dir(root)


def list_runs(root=None, limit=30):
    root = root or ROOT
    runs = root / "runs"
    if not runs.is_dir():
        return []
    out = []
    for path in sorted(runs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        meta = {}
        meta_path = path / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                pass
        out.append({"run_id": path.name, "path": str(path), **meta})
        if len(out) >= limit:
            break
    return out
