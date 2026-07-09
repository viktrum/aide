#!/usr/bin/env python3
"""SessionStart hook — spawn incremental mine.py when the last run is stale.

Returns immediately; the miner runs in a detached background process.
Configure with env vars:
  AIDE_MINE_ENABLED=1          (0 to disable)
  AIDE_MINE_INTERVAL_HOURS=24  min hours between runs
  AIDE_MINE_SINCE_DAYS=7       --since passed to mine.py
  AIDE_MINE_DRY_RUN=1          log intent only (tests)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge_store import LATEST_JSON, ROOT  # noqa: E402

MINE_SCRIPT = Path(__file__).resolve().parent.parent / "miner" / "mine.py"
LOCK_PATH = ROOT / ".mine.lock"
LOG_PATH = ROOT / "scheduled_mine.log"
STALE_LOCK_HOURS = 3


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _enabled() -> bool:
    return os.environ.get("AIDE_MINE_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _parse_generated_at(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return None


def hours_since_last_mine(now: float | None = None) -> float | None:
    """Hours since latest.json generated_at, or None if no prior run."""
    now = now if now is not None else time.time()
    if not LATEST_JSON.exists():
        return None
    try:
        record = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ts = _parse_generated_at(record.get("generated_at"))
    if ts is None:
        return None
    return max(0.0, (now - ts) / 3600.0)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def read_lock() -> dict | None:
    if not LOCK_PATH.exists():
        return None
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def lock_blocks(now: float | None = None) -> bool:
    """True if a mine job appears to be running."""
    now = now if now is not None else time.time()
    lock = read_lock()
    if not lock:
        return False
    pid = int(lock.get("pid") or 0)
    started = float(lock.get("started_at") or 0)
    if _pid_alive(pid):
        return True
    if started and (now - started) < STALE_LOCK_HOURS * 3600:
        return False
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def should_run_mine(
    *,
    now: float | None = None,
    interval_hours: int | None = None,
    enabled: bool | None = None,
) -> tuple[bool, str]:
    if enabled is None:
        enabled = _enabled()
    if not enabled:
        return False, "disabled"

    if lock_blocks(now=now):
        return False, "locked"

    interval = interval_hours if interval_hours is not None else _env_int(
        "AIDE_MINE_INTERVAL_HOURS", 24)
    elapsed = hours_since_last_mine(now=now)
    if elapsed is None:
        return True, "no_prior_run"
    if elapsed >= interval:
        return True, f"stale_{elapsed:.1f}h"
    return False, f"fresh_{elapsed:.1f}h"


def try_acquire_lock(now: float | None = None) -> bool:
    """Atomically create lock file; return True if acquired."""
    now = now if now is not None else time.time()
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "started_at": now,
        }).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def write_lock(pid: int, now: float | None = None) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(json.dumps({
        "pid": pid,
        "started_at": now if now is not None else time.time(),
    }), encoding="utf-8")


def clear_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def spawn_mine_worker(since_days: int | None = None) -> int | None:
    """Start detached worker; returns child PID or None on dry-run."""
    since = since_days if since_days is not None else _env_int("AIDE_MINE_SINCE_DAYS", 7)
    if os.environ.get("AIDE_MINE_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        return None

    if not try_acquire_lock():
        return None

    ROOT.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_PATH, "a", encoding="utf-8")
    log_handle.write(
        f"\n--- scheduled mine started {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(since={since}d) ---\n"
    )
    log_handle.flush()

    env = dict(os.environ)
    env["CLAUDE_JUDGE_HOME"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker",
         "--since", str(since)],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    try:
        LOCK_PATH.write_text(json.dumps({
            "pid": proc.pid,
            "started_at": time.time(),
        }), encoding="utf-8")
    except OSError:
        pass
    return proc.pid


def worker_main(since_days: int) -> int:
    try:
        subprocess.run(
            [sys.executable, str(MINE_SCRIPT), "--since", str(since_days), "--quiet"],
            cwd=str(MINE_SCRIPT.parent),
            env={**os.environ, "CLAUDE_JUDGE_HOME": str(ROOT)},
            check=False,
        )
    finally:
        clear_lock()
    return 0


def hook_main() -> int:
    try:
        json.load(sys.stdin)
    except json.JSONDecodeError:
        pass

    run, reason = should_run_mine()
    if not run:
        sys.exit(0)

    since = _env_int("AIDE_MINE_SINCE_DAYS", 7)
    pid = spawn_mine_worker(since_days=since)
    if pid is None and os.environ.get("AIDE_MINE_DRY_RUN"):
        print(json.dumps({
            "systemMessage": f"[judge/mine] dry-run: would spawn mine ({reason})",
        }))
        sys.exit(0)

    interval = _env_int("AIDE_MINE_INTERVAL_HOURS", 24)
    print(json.dumps({
        "systemMessage": (
            f"[judge/mine] Re-scanning your last {since} day(s) of sessions "
            f"in the background ({reason}; interval {interval}h). "
            f"Log: {LOG_PATH}"
        ),
    }))
    sys.exit(0)


def main() -> int:
    # Optimizer CLI child sessions must never trigger a background re-mine.
    # The detached --worker process is spawned from a real session's env, so
    # this guard cannot strand a legitimate mine.
    if os.environ.get("AIDE_JUDGE_BYPASS"):
        return 0
    if "--worker" in sys.argv:
        since = _env_int("AIDE_MINE_SINCE_DAYS", 7)
        argv = sys.argv[1:]
        for i, arg in enumerate(argv):
            if arg == "--since" and i + 1 < len(argv):
                try:
                    since = int(argv[i + 1])
                except ValueError:
                    pass
                break
        return worker_main(since)
    return hook_main()


if __name__ == "__main__":
    sys.exit(main())
