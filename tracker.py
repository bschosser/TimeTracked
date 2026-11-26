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
APP_VERSION = "2026-11-26.2"
HISTORY_LIMIT = 120_960
# samples.jsonl holds one record per stretch of unchanged state: `timestamp` is when
# the stretch started and `until` the latest sample that confirmed it. Each sample
# either moves `until` forward (rewriting only the last line) or, on a state change,
# appends a new record. A stretch is only extended while samples keep coming; after a
# longer gap (e.g. the Mac was asleep) a new record starts, so the dashboard still sees
# the gap and can cap it at four hours.
MAX_SAMPLE_GAP_SECONDS = max(3 * INTERVAL_SECONDS, 180)
STATE_KEYS = ("mail_open", "working", "paused", "idle", "signal_app",
              "manual_break", "manual_break_id", "manual_break_boundary")

# In-memory copy of samples.jsonl so requests don't re-read the whole file.
# The (mtime, size) stamp detects writes made outside this process; the
# revision changes whenever past records may have changed, so clients that
# only fetch new samples know to reload everything.
_history_cache: list[dict] = []
_history_stamp: tuple[int, int] | None = None
_history_revision = uuid4().hex
# The most recent raw sample (with idle_seconds), shown as the current status.
_latest_sample: dict | None = None


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
    """Mark records starting in [start, end) as not working because nobody was at the computer.

    Split stretches at `start` and `end` first (split_at) so they line up with the edges.
    """
    changed = 0
    for record in records:
        if start <= history_timestamp(record) < end and not record.get("manual_break"):
            if record.get("working", record.get("mail_open")):
                changed += 1
            record["working"] = False
            record["idle"] = True
    return changed


def record_until(record: dict) -> datetime:
    return datetime.fromisoformat(record.get("until", record["timestamp"]).replace("Z", "+00:00"))


def same_state(a: dict, b: dict) -> bool:
    return all(a.get(key) == b.get(key) for key in STATE_KEYS)


def can_extend(segment: dict, record: dict) -> bool:
    gap = (history_timestamp(record) - record_until(segment)).total_seconds()
    return same_state(segment, record) and 0 <= gap <= MAX_SAMPLE_GAP_SECONDS


def as_segment(record: dict) -> dict:
    segment = {key: value for key, value in record.items() if key != "idle_seconds"}
    segment.setdefault("until", segment["timestamp"])
    return segment


def compact(records: list[dict]) -> list[dict]:
    """Merge consecutive records with the same state into stretches (also converts old per-sample files)."""
    result: list[dict] = []
    for record in records:
        record = as_segment(record)
        previous = result[-1] if result else None
        if previous is not None and can_extend(previous, record):
            if record_until(record) > record_until(previous):
                previous["until"] = record["until"]
        else:
            result.append(record)
    return result


def split_at(records: list[dict], moment: datetime) -> None:
    """Split the stretch that covers `moment` so a new record starts exactly there."""
    if any(history_timestamp(record) == moment for record in records):
        return
    for index, record in enumerate(records):
        if history_timestamp(record) < moment <= record_until(record):
            tail = dict(record, timestamp=moment.astimezone(timezone.utc).isoformat())
            tail.pop("manual_break_boundary", None)  # the boundary stays at the stretch's start
            if not record.get("manual_break"):
                tail.pop("manual_break_id", None)  # only the inside of a break carries its id
            record["until"] = tail["timestamp"]
            records.insert(index + 1, tail)
            return


def append_sample() -> dict:
    global _history_stamp, _latest_sample
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_LOCK:
        cache = cached_history()
        record = sample()
        _latest_sample = record
        previous = cache[-1] if cache else None
        if record["idle"] and previous is not None and not previous.get("idle"):
            # Just crossed the inactivity limit: nobody has been at the computer since the
            # last input, so take the time since then off as well (once, on the transition).
            now = history_timestamp(record)
            records = read_history_file()
            idle_start = now - timedelta(seconds=record.get("idle_seconds", 0))
            split_at(records, idle_start)
            mark_away(records, idle_start, now + timedelta(days=1))
            records.append(as_segment(record))
            write_history(records)
            return record
        if previous is not None and can_extend(previous, record):
            previous["until"] = record["timestamp"]
            if not rewrite_last_line(previous):
                # The file doesn't end cleanly (e.g. an interrupted write): rewrite it whole.
                records = read_history_file()
                if records and records[-1].get("timestamp") == previous["timestamp"]:
                    records[-1] = previous
                else:
                    records.append(previous)
                write_history(records)
                return record
        else:
            segment = as_segment(record)
            with HISTORY_FILE.open("a", encoding="utf-8") as history:
                history.write(json.dumps(segment) + "\n")
            cache.append(segment)
            if len(cache) > HISTORY_LIMIT + 1_000:
                del cache[:-HISTORY_LIMIT]
        _history_stamp = history_file_stamp()
    return record


def rewrite_last_line(record: dict) -> bool:
    """Replace the file's last line with `record`; False if the file doesn't end cleanly."""
    try:
        with HISTORY_FILE.open("r+b") as history:
            size = history.seek(0, os.SEEK_END)
            if size == 0:
                return False
            history.seek(size - 1)
            if history.read(1) != b"\n":
                return False
            position, start = size - 1, 0
            while position > 0:
                chunk_start = max(0, position - 4096)
                history.seek(chunk_start)
                newline = history.read(position - chunk_start).rfind(b"\n")
                if newline >= 0:
                    start = chunk_start + newline + 1
                    break
                position = chunk_start
            history.seek(start)
            history.truncate()
            history.write((json.dumps(record) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


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
    records = compact(records)
    with HISTORY_LOCK:
        with HISTORY_FILE.open("w", encoding="utf-8") as history:
            history.writelines(json.dumps(record) + "\n" for record in records)
        _history_cache = records[-HISTORY_LIMIT:]
        _history_stamp = history_file_stamp()
        _history_revision = uuid4().hex


def latest_sample() -> dict | None:
    with HISTORY_LOCK:
        if _latest_sample is not None:
            return _latest_sample
        cache = cached_history()
        if not cache:
            return None
        return dict(cache[-1], timestamp=cache[-1].get("until", cache[-1]["timestamp"]))


def migrate_history() -> None:
    """Convert an old file with one line per sample into stretches (keeps a backup)."""
    with HISTORY_LOCK:
        records = read_history_file()
        if not records or all("until" in record for record in records):
            return
        backup = HISTORY_FILE.with_name(HISTORY_FILE.name + ".pre-stretches")
        if not backup.exists():
            backup.write_bytes(HISTORY_FILE.read_bytes())
        write_history(records)
        print(f"Compacted {len(records)} samples into {len(_history_cache)} stretches (backup: {backup.name}).")


def history_since(since: str | None, revision: str | None) -> tuple[list[dict], str, bool]:
    """Stretches starting at or after `since` (the client's last one may have grown), or
    the full history if the client's copy is stale."""
    with HISTORY_LOCK:
        records = cached_history()
        current_revision = _history_revision
        if not since or revision != current_revision:
            return records[-HISTORY_LIMIT:], current_revision, True
        try:
            since_time = datetime.fromisoformat(since.replace("Z", "+00:00"))
            start = len(records)
            while start > 0 and history_timestamp(records[start - 1]) >= since_time:
                start -= 1
        except (KeyError, TypeError, ValueError):
            return records[-HISTORY_LIMIT:], current_revision, True
        return records[start:], current_revision, False


def history_timestamp(record: dict) -> datetime:
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def parse_correction_range(start_value: str, end_value: str) -> tuple[datetime, datetime]:
    """Validate a past time range sent by the dashboard's correction form."""
    try:
        start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ManualBreakError("Choose a valid start and end time.") from error
    if start.tzinfo is None or end.tzinfo is None:
        raise ManualBreakError("Choose a valid start and end time.")
    if start >= end:
        raise ManualBreakError("The end time must be after the start time.")
    if end > datetime.now().astimezone():
        raise ManualBreakError("The time range must have ended already.")
    return start, end


def mark_not_working(start_value: str, end_value: str) -> dict:
    """Take a past time range off (e.g. Mail was left open), like --mark-away."""
    start, end = parse_correction_range(start_value, end_value)
    with HISTORY_LOCK:
        records = read_history_file()
        records.sort(key=history_timestamp)
        split_at(records, start)
        split_at(records, end)
        changed = mark_away(records, start, end)
        if changed:
            write_history(records)
    return {"start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(), "changed": changed}


def add_manual_break(start_value: str, end_value: str) -> dict:
    start, end = parse_correction_range(start_value, end_value)

    with HISTORY_LOCK:
        records = read_history_file()
        records.sort(key=history_timestamp)
        # Stretches can span the break's edges; split them so a record starts at each edge.
        split_at(records, start)
        split_at(records, end)

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
                if request.get("kind") == "not_working":
                    interval = mark_not_working(request.get("start"), request.get("end"))
                else:
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
        split_at(records, start)
        split_at(records, end)
        changed = mark_away(records, start, end)
        write_history(records)
    print(f"Marked {changed} tracked stretches between {start:%a %Y-%m-%d %H:%M} and {end:%a %Y-%m-%d %H:%M} as away (not working).")


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
    migrate_history()
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
