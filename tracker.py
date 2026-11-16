#!/usr/bin/env python3
"""Private local working-hours tracker for macOS."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "samples.jsonl"
PAUSE_FILE = DATA_DIR / "pause.json"
INTERVAL_SECONDS = int(os.getenv("TRACK_INTERVAL", "60"))
MAIL_APP = os.getenv("MAIL_APP", "Mail")
HISTORY_LOCK = threading.RLock()


class ManualBreakError(ValueError):
    pass


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_mail_open() -> bool:
    result = subprocess.run(["pgrep", "-x", MAIL_APP], capture_output=True, timeout=3)
    return result.returncode == 0


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
        records = read_history()
        changed = False
        for record in records:
            if record.get("timestamp", "") >= started_at and record.get("paused") and not record.get("manual_break"):
                record["paused"] = False
                record["working"] = record.get("mail_open", False)
                changed = True
        if changed:
            with HISTORY_FILE.open("w", encoding="utf-8") as history:
                history.writelines(json.dumps(record) + "\n" for record in records)


def sample() -> dict:
    mail_open = is_mail_open()
    paused = is_paused()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mail_open": mail_open,
        "working": mail_open and not paused,
        "paused": paused,
    }


def append_sample() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    with HISTORY_LOCK:
        record = sample()
        with HISTORY_FILE.open("a", encoding="utf-8") as history:
            history.write(json.dumps(record) + "\n")
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


def read_history() -> list[dict]:
    records = read_history_file()
    return records[-120_960:]


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
        end_record = boundary_record(end)
        if not end_record:
            end_record = {"timestamp": end.astimezone(timezone.utc).isoformat(), "mail_open": mail_open_at_end}
            records.append(end_record)
        end_record.update({
            "working": mail_open_at_end and not paused_at_end,
            "paused": paused_at_end,
            "manual_break": False,
            "manual_break_id": break_id,
            "manual_break_boundary": "end",
        })

        records.sort(key=history_timestamp)
        with HISTORY_FILE.open("w", encoding="utf-8") as history:
            history.writelines(json.dumps(record) + "\n" for record in records)

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
            records = read_history()
            self.send_json({"current": records[-1] if records else append_sample(), "interval": INTERVAL_SECONDS, "mail_app": MAIL_APP, "paused": is_paused()})
            return
        if path == "/api/history":
            self.send_json({"samples": read_history(), "interval": INTERVAL_SECONDS})
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
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
            self.send_error(404)
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
        DATA_DIR.mkdir(exist_ok=True)
        pause_data: dict[str, object] = {"paused": paused}
        if paused:
            pause_data["started_at"] = datetime.now(timezone.utc).isoformat()
        PAUSE_FILE.write_text(json.dumps(pause_data), encoding="utf-8")
        self.send_json({"paused": paused, "sample": append_sample()})

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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8765"))
    append_sample()
    threading.Thread(target=sampler_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Working hours tracker running at http://127.0.0.1:{port}")
    print(f"Sampling every {INTERVAL_SECONDS}s; watching {MAIL_APP}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
