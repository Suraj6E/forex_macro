"""Keeps the economic calendar current while the simulator is open.

The calendar lives in fxmacro's database and is collected by fxmacro's own
code; the simulator reads it and never writes there. Collection used to
happen only when someone pressed Fetch in fxmacro, so the calendar stopped at
30 Sep 2026 and every chart after that had no events. Now the simulator runs
fxmacro's `manage.py refresh_calendar` in a child process:

  * shortly after the server starts;
  * every three hours;
  * three minutes after each medium or high impact release of USD, EUR, GBP
    or JPY, so a live session gets the actual within minutes;
  * when **Update now** is pressed in Data.

What a run fetches, and how politely, is fxmacro's decision
(sources/calendar_refresh.py): the ForexFactory weekly feed once a day
(this week's schedule and point-in-time forecasts), the month pages in full
once a day (every month since the last full refresh up to next month), and
otherwise only the events around today, never twice in ten minutes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

from simulator import config

FIRST_DELAY_S = 20
EVERY_S = 3 * 3600
AFTER_RELEASE_S = 180
#: The refresh skips the pages if they were fetched in the last ten minutes;
#: asking sooner would only start a process for nothing.
MIN_GAP_S = 600
RETRY_S = 1800
TIMEOUT_S = 900
#: Releases rated at least this wake the updater (2: medium and high).
WAKE_IMPACT = 2


def next_run_s(last_run_s: float, next_release_s: float | None, failed: bool = False) -> float:
    """When to run next (epoch seconds), after a run at `last_run_s`: three
    hours later or three minutes after the next release, whichever comes
    first, but never within ten minutes of the last run. After a failure,
    half an hour later, whatever is released meanwhile."""
    due = last_run_s + (RETRY_S if failed else EVERY_S)
    if next_release_s is not None and not failed:
        due = min(due, next_release_s + AFTER_RELEASE_S)
    return max(due, last_run_s + MIN_GAP_S)


def _plain(text: str) -> str:
    """fxmacro's fetch summary without the parts that are nearly always zero
    or only housekeeping."""
    text = re.sub(r", 0 (with a cross-source disagreement|newly unmapped)", "", text or "")
    return re.sub(r"; \d+ older snapshot\(s\) of the same pages replaced \([^)]*\)", "", text)


def summarise(out: dict) -> str:
    """One line from the refresh's JSON summary."""
    parts = []
    if out.get("pages_mode") != "skip":
        parts.append(_plain(out.get("pages", "")))
    if out.get("weekly", "").startswith("weekly feed:") and "next allowed" not in out.get("weekly", ""):
        parts.append(_plain(out["weekly"]))
    twins = out.get("twins") or ""
    if twins and not twins.startswith("0 "):
        parts.append(twins)
    if not parts:
        return "Nothing was due: " + "; ".join(x for x in (out.get("pages"), out.get("weekly")) if x)
    return "; ".join(p for p in parts if p)


class CalendarUpdater:
    def __init__(self, calendar, command: list[str] | None = None, enabled: bool | None = None) -> None:
        self.calendar = calendar
        manage = config.REPO_DIR / "manage.py"
        self.command = command or [sys.executable, str(manage), "refresh_calendar", "--json"]
        reason = None
        if enabled is None:
            if not config.CALENDAR_UPDATE:
                reason = "Off (SIM_CALENDAR_UPDATE=0)."
            elif calendar.path.resolve() != (config.REPO_DIR / "db.sqlite3").resolve():
                reason = "Off: the calendar is read from another database than fxmacro's own."
            elif not manage.exists():
                reason = f"Off: fxmacro's manage.py was not found at {manage}."
        elif not enabled:
            reason = "Off."
        self.enabled = reason is None
        self.status: dict = {"state": "waiting" if self.enabled else "off", "message": reason,
                             "last_ms": None, "ok_ms": None, "next_ms": None}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self._thread: threading.Thread | None = None

    # -- control ---------------------------------------------------------------

    def start(self, first_delay_s: float = FIRST_DELAY_S) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self.status["next_ms"] = int((time.time() + first_delay_s) * 1000)
        self._thread = threading.Thread(target=self._loop, args=(first_delay_s,), name="calendar-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_now(self) -> bool:
        """Ask for a run straight away (the pages' ten-minute gap is skipped;
        the weekly feed's daily limit still holds). False when switched off."""
        if not self.enabled:
            return False
        if self.status["state"] != "running":
            self._force = True
            self._wake.set()
        return True

    # -- the loop --------------------------------------------------------------

    def _loop(self, first_delay_s: float) -> None:
        self._wake.wait(first_delay_s)
        while not self._stop.is_set():
            self._wake.clear()
            failed = not self.run_once(force=self._force)
            self._force = False
            last = time.time()
            while not self._stop.is_set():
                nxt = self.calendar.next_after(int((last - AFTER_RELEASE_S) * 1e9), WAKE_IMPACT)
                due = next_run_s(last, nxt / 1e9 if nxt is not None else None, failed)
                self.status["next_ms"] = int(due * 1000)
                wait = due - time.time()
                if wait <= 0:
                    break
                # Looked at again each minute: a refresh may add releases.
                if self._wake.wait(min(wait, 60)):
                    break

    def run_once(self, force: bool = False) -> bool:
        """One refresh, here and now. True when it succeeded."""
        self.status.update(state="running", message="Updating the calendar from ForexFactory…")
        cmd = list(self.command) + (["--force"] if force else [])
        env = dict(os.environ, FXMACRO_WORKER="0", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.run(cmd, cwd=str(config.REPO_DIR), env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            return self._done(False, f"Could not run fxmacro's refresh: {type(exc).__name__}: {exc}")
        out = None
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.startswith("{"):
                try:
                    out = json.loads(line)
                except ValueError:
                    pass
                break
        self.calendar.invalidate()
        if proc.returncode != 0:
            err = [x for x in (proc.stderr or "").strip().splitlines() if x.strip()]
            detail = (out and "; ".join(out.get("errors") or [])) or (err[-1] if err else f"exit code {proc.returncode}")
            return self._done(False, f"The calendar update failed: {detail}")
        return self._done(True, summarise(out or {}))

    def _done(self, ok: bool, message: str) -> bool:
        now_ms = int(time.time() * 1000)
        self.status.update(state="ok" if ok else "error", message=message, last_ms=now_ms)
        if ok:
            self.status["ok_ms"] = now_ms
        return ok
