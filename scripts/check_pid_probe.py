"""What does os.kill(pid, 0) actually do on this host?

scheduler/daily_scrape.py:_pid_alive() uses os.kill(pid, 0) to decide whether a
stale lock file belongs to a live scrape. Two things must hold:

  1. probing a LIVE pid must not disturb it, and must not raise
  2. probing a DEAD pid must raise, or a lock left by a crashed run is never
     recognised as stale and every future run refuses to start

On POSIX signal 0 is a pure liveness probe and both hold. On Windows,
signal.CTRL_C_EVENT == 0, so os.kill(pid, 0) is not a probe -- it asks the OS to
deliver a console Ctrl+C, which lands in this process's own console group as an
asynchronous KeyboardInterrupt. Every probe and sleep below is shielded because
of that.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

INTERRUPTED: list[str] = []


def patient_sleep(seconds: float) -> None:
    """Sleep through an async console Ctrl+C instead of dying from it."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            INTERRUPTED.append("sleep")


def probe(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except BaseException as exc:  # KeyboardInterrupt is not an Exception
        return f"raised {type(exc).__name__}: {exc}"
    return "returned without raising"


def spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    print(f"platform={sys.platform} python={sys.version.split()[0]}")
    ctrl_c = getattr(signal, "CTRL_C_EVENT", None)
    print(f"signal.CTRL_C_EVENT = {ctrl_c}  (is signal 0? {ctrl_c == 0})\n")

    # Dead pid first: a process that exits on its own, so nothing is killed.
    short = spawn("pass")
    for _ in range(40):
        if short.poll() is not None:
            break
        patient_sleep(0.25)
    patient_sleep(1.0)
    print(f"-- dead pid {short.pid} (exited {short.poll()})")
    dead_result = probe(short.pid)
    print(f"   probe: {dead_result}")

    # Live pid second.
    long = spawn("import time; time.sleep(20)")
    patient_sleep(1.5)
    print(f"\n-- live pid {long.pid}")
    print(f"   alive before: {long.poll() is None}")
    live_result = probe(long.pid)
    print(f"   probe: {live_result}")
    patient_sleep(1.5)
    survived = long.poll() is None
    print(f"   alive after:  {survived}")
    long.terminate()

    print(f"\n-- async KeyboardInterrupts absorbed by this process: {len(INTERRUPTED)}")

    print("\n-- verdict for _pid_alive()")
    problems = []
    if "raised" not in dead_result:
        problems.append(
            "a DEAD pid reports alive -> a lock left by a crashed run never "
            "clears, so every later run aborts with 'Another daily scrape is "
            "running'"
        )
    if not survived:
        problems.append("probing a LIVE pid killed it")
    if ctrl_c == 0:
        problems.append(
            "signal 0 is CTRL_C_EVENT here, so the 'probe' delivers a console "
            "Ctrl+C to the process group rather than testing liveness"
        )
    for problem in problems:
        print(f"   BROKEN: {problem}")
    if not problems:
        print("   OK: behaves as a liveness probe")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n(parent absorbed a trailing console Ctrl+C)")
        raise SystemExit(0)
