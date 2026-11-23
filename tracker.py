#!/usr/bin/env python3
"""Private local working-hours tracker for macOS."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("TRACKER_DATA_DIR", str(ROOT / "data")))
HISTORY_FILE = DATA_DIR / "samples.jsonl"
PAUSE_FILE = DATA_DIR / "pause.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
INTERVAL_SECONDS = int(os.getenv("TRACK_INTERVAL", "60"))
DEFAULT_MAIL_APP = os.getenv("MAIL_APP", "Mail").strip() or "Mail"
# After this many minutes without keyboard/mouse input, time no longer counts as work,
# even if the signal app is still open (e.g. Mail left open over the weekend), and the
# inactive time since the last input is taken off as well. Shorter inactivity (meetings,
# reading) keeps counting.
IDLE_MINUTES = float(os.getenv("IDLE_MINUTES", str(4 * 60)))
HISTORY_LOCK = threading.RLock()
# Keep in sync with APP_VERSION in app.js; the dashboard footer compares both.
APP_VERSION = "2026-11-23.1"
HISTORY_LIMIT = 120_960

# In-memory copy of samples.jsonl so requests don't re-read the whole file.
# The (mtime, size) stamp detects writes made outside this process; the
# revision changes whenever past records may have changed, so clients that
# only fetch new samples know to reload everything.
_history_cache: list[dict] = []
_history_stamp: tuple[int, int] | None = None
_history_revision = uuid4().hex


class ManualBreakError(ValueError):
    pass


class SignalAppError(ValueError):
    pass


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def signal_app() -> str:
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        configured = settings.get("mail_app") if isinstance(settings, dict) else None
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return DEFAULT_MAIL_APP


def set_signal_app(value: object) -> str:
    if not isinstance(value, str):
        raise SignalAppError("Enter the app's process name.")
    name = value.strip()
    if not name or len(name) > 100 or any(char in name for char in "\r\n\0"):
        raise SignalAppError("Enter a valid app process name (1–100 characters).")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({"mail_app": name}), encoding="utf-8")
    return name


def is_app_open(app_name: str) -> bool:
    try:
        result = subprocess.run(["pgrep", "-x", app_name], capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input on macOS, or None if unknown."""
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', command_output(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    return int(match.group(1)) / 1e9 if match else None


def is_paused() -> bool:
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8")).get("paused", False)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def pause_started_at() -> str | None:
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8")).get("started_at")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def reclassify_pause(started_at: str | None) -> None:
    if not started_at or not HISTORY_FILE.exists():
        return
    with HISTORY_LOCK:
        records = read_history_file()
        changed = False
        for record in records:
            if record.get("timestamp", "") >= started_at and record.get("paused") and not record.get("manual_break"):
                record["paused"] = False
                record["working"] = record.get("mail_open", False) and not record.get("idle", False)
                changed = True
        if changed:
            write_history(records)


def sample() -> dict:
    app_name = signal_app()
    mail_open = is_app_open(app_name)
    paused = is_paused()
    idle_for = idle_seconds()
    idle = idle_for is not None and idle_for >= IDLE_MINUTES * 60
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mail_open": mail_open,
        "working": mail_open and not paused and not idle,
        "paused": paused,
        "idle": idle,
        "signal_app": app_name,
    }
    if idle_for is not None:
        record["idle_seconds"] = round(idle_for)
    return record


def mark_away(records: list[dict], start: datetime, end: datetime) -> int:
    """Mark samples in [start, end) as not working because nobody was at the computer."""
    changed = 0
    for record in records:
        if start <= history_timestamp(record) < end and not record.get("manual_break"):
            if record.get("working", record.get("mail_open")):
                changed += 1
            record["working"] = False
            record["idle"] = True
    return changed


def append_sample() -> dict:
    global _history_stamp
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_LOCK:
        cache = cached_history()
        record = sample()
        previous = cache[-1] if cache else None
        if record["idle"] and previous is not None and not previous.get("idle"):
            # Just crossed the inactivity limit: nobody has been at the computer since the
            # last input, so take the samples since then off as well (once, on the transition).
            now = datetime.now(timezone.utc)
            records = read_history_file()
            mark_away(records, now - timedelta(seconds=record.get("idle_seconds", 0)), now + timedelta(days=1))
            records.append(record)
            write_history(records)
            return record
        with HISTORY_FILE.open("a", encoding="utf-8") as history:
            history.write(json.dumps(record) + "\n")
        cache.append(record)
        if len(cache) > HISTORY_LIMIT + 1_000:
            del cache[:-HISTORY_LIMIT]
        _history_stamp = history_file_stamp()
    return record


def read_history_file() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    records = []
    with HISTORY_LOCK:
        with HISTORY_FILE.open(encoding="utf-8") as history:
            for line in history:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def history_file_stamp() -> tuple[int, int] | None:
    try:
        stat = HISTORY_FILE.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def cached_history() -> list[dict]:
    """Return the live cache list; callers must hold HISTORY_LOCK while using it."""
    global _history_cache, _history_stamp, _history_revision
    with HISTORY_LOCK:
        stamp = history_file_stamp()
        if stamp != _history_stamp:
            _history_cache = read_history_file()[-HISTORY_LIMIT:]
            _history_stamp = stamp
            _history_revision = uuid4().hex
        return _history_cache


def write_history(records: list[dict]) -> None:
    global _history_cache, _history_stamp, _history_revision
    with HISTORY_LOCK:
        with HISTORY_FILE.open("w", encoding="utf-8") as history:
            history.writelines(json.dumps(record) + "\n" for record in records)
        _history_cache = records[-HISTORY_LIMIT:]
        _history_stamp = history_file_stamp()
        _history_revision = uuid4().hex


def latest_sample() -> dict | None:
    with HISTORY_LOCK:
        cache = cached_history()
        return cache[-1] if cache else None


def history_since(since: str | None, revision: str | None) -> tuple[list[dict], str, bool]:
    """Samples newer than `since`, or the full history if the client's copy is stale."""
    with HISTORY_LOCK:
        records = cached_history()
        current_revision = _history_revision
        if not since or revision != current_revision:
            return records[-HISTORY_LIMIT:], current_revision, True
        try:
            since_time = datetime.fromisoformat(since.replace("Z", "+00:00"))
            start = len(records)
            while start > 0 and history_timestamp(records[start - 1]) > since_time:
                start -= 1
        except (KeyError, TypeError, ValueError):
            return records[-HISTORY_LIMIT:], current_revision, True
        return records[start:], current_revision, False


def history_timestamp(record: dict) -> datetime:
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def add_manual_break(start_value: str, end_value: str) -> dict:
    try:
        start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ManualBreakError("Choose a valid start and end time.") from error
    if start.tzinfo is None or end.tzinfo is None:
        raise ManualBreakError("Choose a valid start and end time.")

    now = datetime.now().astimezone()
    if start >= end:
        raise ManualBreakError("The end time must be after the start time.")
    if start.astimezone().date() != now.date() or end.astimezone().date() != now.date():
        raise ManualBreakError("A missed break can only be added for today.")
    if end > now:
        raise ManualBreakError("The break must have ended already.")

    with HISTORY_LOCK:
        records = read_history_file()
        records.sort(key=history_timestamp)

        existing_breaks: dict[str, dict[str, datetime]] = {}
        for record in records:
            break_id = record.get("manual_break_id")
            boundary = record.get("manual_break_boundary")
            if break_id and boundary in {"start", "end"}:
                existing_breaks.setdefault(break_id, {})[boundary] = history_timestamp(record)
        for interval in existing_breaks.values():
            old_start, old_end = interval.get("start"), interval.get("end")
            if old_start and old_end and start < old_end and old_start < end:
                raise ManualBreakError("That time overlaps a break already added today.")

        for record in records:
            timestamp = history_timestamp(record)
            if start <= timestamp < end and record.get("paused") and not record.get("manual_break_id"):
                raise ManualBreakError("That time overlaps a break already recorded.")

        mail_open_at_start = False
        end_source = None
        for record in records:
            timestamp = history_timestamp(record)
            if timestamp < start:
                mail_open_at_start = bool(record.get("mail_open", record.get("working", False)))
            if timestamp <= end:
                end_source = record

        break_id = uuid4().hex
        for record in records:
            timestamp = history_timestamp(record)
            if start <= timestamp < end:
                record["working"] = False
                record["paused"] = True
                record["manual_break"] = True
                record["manual_break_id"] = break_id

        def boundary_record(timestamp: datetime) -> dict:
            return next((record for record in records if history_timestamp(record) == timestamp and not record.get("manual_break_boundary")), {})

        start_record = boundary_record(start)
        if not start_record:
            start_record = {"timestamp": start.astimezone(timezone.utc).isoformat(), "mail_open": mail_open_at_start}
            records.append(start_record)
        start_record.update({
            "working": False,
            "paused": True,
            "manual_break": True,
            "manual_break_id": break_id,
            "manual_break_boundary": "start",
        })

        mail_open_at_end = bool(end_source.get("mail_open", end_source.get("working", False))) if end_source else mail_open_at_start
        paused_at_end = bool(end_source.get("paused", False)) if end_source else False
        idle_at_end = bool(end_source.get("idle", False)) if end_source else False
        end_record = boundary_record(end)
        if not end_record:
            end_record = {"timestamp": end.astimezone(timezone.utc).isoformat(), "mail_open": mail_open_at_end}
            records.append(end_record)
        end_record.update({
            "working": mail_open_at_end and not paused_at_end and not idle_at_end,
            "paused": paused_at_end,
            "manual_break": False,
            "manual_break_id": break_id,
            "manual_break_boundary": "end",
        })

        records.sort(key=history_timestamp)
        write_history(records)

    return {"start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat()}


def sampler_loop() -> None:
    while True:
        append_sample()
        time.sleep(INTERVAL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            current = latest_sample()
            self.send_json({"current": current if current else append_sample(), "interval": INTERVAL_SECONDS, "mail_app": signal_app(), "paused": is_paused(), "version": APP_VERSION})
            return
        if path == "/api/settings":
            self.send_json({"mail_app": signal_app()})
            return
        if path == "/api/history":
            query = parse_qs(urlparse(self.path).query)
            samples, revision, full = history_since(query.get("since", [None])[0], query.get("revision", [None])[0])
            self.send_json({"samples": samples, "interval": INTERVAL_SECONDS, "revision": revision, "full": full})
            return
        if path.startswith("/api/"):
            self.send_json({"error": f"Unknown endpoint {path}."}, status=404)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        try:
            self.handle_post()
        except Exception as error:  # noqa: BLE001 - always answer API calls with JSON
            self.send_json({"error": f"Tracker error: {error}"}, status=500)

    def handle_post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/settings":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                request = json.loads(body or b"{}")
                if not isinstance(request, dict):
                    raise SignalAppError("Enter the app's process name.")
                app_name = set_signal_app(request.get("mail_app"))
            except (json.JSONDecodeError, SignalAppError) as error:
                self.send_json({"error": str(error) or "Enter the app's process name."}, status=400)
                return
            self.send_json({"ok": True, "mail_app": app_name, "sample": append_sample()})
            return
        if path == "/api/manual-break":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                request = json.loads(body or b"{}")
                if not isinstance(request, dict):
                    raise ManualBreakError("Choose a valid start and end time.")
                interval = add_manual_break(request.get("start"), request.get("end"))
            except (json.JSONDecodeError, ManualBreakError) as error:
                self.send_json({"error": str(error) or "Choose a valid start and end time."}, status=400)
                return
            self.send_json({"ok": True, "break": interval})
            return
        if path not in {"/api/pause", "/api/resume"}:
            self.send_json({"error": f"Unknown endpoint {path}."}, status=404)
            return
        if path == "/api/resume":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            request = json.loads(body or b"{}")
            was_break = request.get("was_break", True)
            if not was_break:
                reclassify_pause(pause_started_at())
            paused = False
        else:
            paused = not is_paused()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pause_data: dict[str, object] = {"paused": paused}
        if paused:
            pause_data["started_at"] = datetime.now(timezone.utc).isoformat()
        PAUSE_FILE.write_text(json.dumps(pause_data), encoding="utf-8")
        self.send_json({"paused": paused, "sample": append_sample()})

    def end_headers(self) -> None:
        # Make the browser revalidate app.js/index.html/styles.css on every load
        # (cheap 304 when unchanged) so an updated checkout is picked up at once.
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def parse_local_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a time like '2026-11-20 13:00', got {value!r}") from error
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def mark_away_command(start: datetime, end: datetime) -> None:
    if start >= end:
        raise SystemExit("The end time must be after the start time.")
    with HISTORY_LOCK:
        records = read_history_file()
        changed = mark_away(records, start, end)
        write_history(records)
    print(f"Marked {changed} samples between {start:%a %Y-%m-%d %H:%M} and {end:%a %Y-%m-%d %H:%M} as away (not working).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Private local working-hours tracker.")
    parser.add_argument("--mark-away", nargs=2, metavar=("START", "END"), type=parse_local_time,
                        help="mark a past period as not working, e.g. --mark-away '2026-11-20 13:00' '2026-11-23 08:00' (local time)")
    args = parser.parse_args()
    if args.mark_away:
        mark_away_command(*args.mark_away)
        return

    port = int(os.getenv("PORT", "8765"))
    # Bind first: if another tracker already uses the port, exit before recording anything.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    append_sample()
    threading.Thread(target=sampler_loop, daemon=True).start()
    print(f"Working hours tracker running at http://127.0.0.1:{port}")
    print(f"Sampling every {INTERVAL_SECONDS}s; watching {signal_app()}")
    print(f"Idle after {IDLE_MINUTES:g} minutes without keyboard or mouse input")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
