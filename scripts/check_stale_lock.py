"""Does a lock left by a killed run block the next one?

Real-world check: a scrape was killed mid-run, leaving scheduler/daily_scrape.lock
holding a pid that no longer exists. Under the old os.kill(pid, 0) probe that pid
reported as alive on Windows, so every later run would have refused to start.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler import daily_scrape

lock = daily_scrape.LOCK_PATH
if not lock.exists():
    print("no lock file present, nothing to test")
    raise SystemExit(0)

stale_pid = int(lock.read_text(encoding="utf-8").strip())
print(f"lock file holds pid {stale_pid}")
print(f"_pid_alive({stale_pid}) = {daily_scrape._pid_alive(stale_pid)}  (want False)")

acquired = daily_scrape.acquire_lock()
print(f"acquire_lock() = {acquired}  (want True)")
if acquired:
    print(f"lock now held by pid {lock.read_text(encoding='utf-8').strip()} (this process)")
    daily_scrape.release_lock()
    print(f"released, lock file gone: {not lock.exists()}")
raise SystemExit(0 if acquired else 1)
