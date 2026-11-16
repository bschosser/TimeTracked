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

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "samples.jsonl"
PAUSE_FILE = DATA_DIR / "pause.json"
INTERVAL_SECONDS = int(os.getenv("TRACK_INTERVAL", "60"))
MAIL_APP = os.getenv("MAIL_APP", "Mail")


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
    records = read_history()
    changed = False
    for record in records:
        if record.get("timestamp", "") >= started_at and record.get("paused"):
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
    record = sample()
    DATA_DIR.mkdir(exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as history:
        history.write(json.dumps(record) + "\n")
    return record


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    records = []
    with HISTORY_FILE.open(encoding="utf-8") as history:
        for line in history:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-120_960:]


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

    def send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
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
