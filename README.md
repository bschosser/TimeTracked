# Desk Signal

A private, lightweight working-hours tracker for macOS. Once a minute it checks whether your work app (Mail by default) is running and turns that into daily, weekly and average working hours. Everything stays on your Mac.

![Desk Signal dashboard](examples/dashboard.png)

## Requirements

- macOS 13 (Ventura) or newer, Apple Silicon or Intel.
- `python3` 3.9 or newer: from Homebrew, or the one in Apple's Command Line Tools (`xcode-select --install`).

## Getting started

The repository includes a ready-built menu-bar app. Clone the project and start it:

```sh
git clone https://github.com/bschosser/TimeTracked.git
cd TimeTracked
nohup build/DeskSignal.app/Contents/MacOS/DeskSignal > /tmp/DeskSignal.log 2>&1 </dev/null &
```

A **TT** icon appears in the menu bar and starts the tracker. Choose **Open dashboard** there, or go to http://127.0.0.1:8765. Leave the app in `build/` inside the project folder: it starts `tracker.py` from there.

Only if you change `DeskSignal.swift` or the included app does not open, rebuild it with `./build_menubar_app.sh` (needs the Command Line Tools).

Without the menu-bar app, run the tracker on its own:

```sh
nohup python3 tracker.py > /tmp/DeskSignal-tracker.log 2>&1 </dev/null &
```

Stop it with `pkill -f '[p]ython3 tracker.py'`. Run only one of the two; both use port 8765.

## Using it

- **Choose the work app:** click **Change app** in the dashboard and enter the app's process name (e.g. `Mail`, `Zulip`, `Code`). Time counts while that app is running, whether or not it is in the foreground.
- **Pause:** use **Pause tracking** in the **TT** menu or the button on the dashboard. When you resume, you choose whether the pause was a break or should count as work.
- **Correct a time range:** use **Correct a time range** at the bottom of the dashboard to fix the day you are looking at. Mark a start and end time as a **Break** (forgot to pause) or as **Not working** (e.g. Mail was left open while you were away; this does the same as `--mark-away` below and is not counted as a break).
- **Browse:** the arrows step through past days and weeks; the **Average day** chart covers 7, 14, 30 or 90 days, or all history. The footer has a **Palette** selector for the color scheme.

## Good to know

- The menu-bar app uses Homebrew's `python3` if installed, otherwise the system one; set `DESK_SIGNAL_PYTHON=/path/to/python3` to choose another.
- Data is stored in the `data/` folder of the project (ignored by git). `samples.jsonl` has one line per stretch of unchanged state (start `timestamp`, last confirming sample `until`); each minute's sample only moves `until` forward, and a new line is added when something changes or after a gap such as sleep. An older file with one line per sample is converted when the tracker starts, and the original is kept as `samples.jsonl.pre-stretches`.
- Gaps between samples (e.g. the Mac was asleep) count for at most four hours.
- Time without keyboard or mouse input keeps counting (meetings, reading) unless it lasts four hours: then the whole inactive stretch is taken off, so Mail left open over a weekend stops counting when you left. Change the limit by starting the app or tracker with e.g. `IDLE_MINUTES=180`.
- For a period longer than one day (e.g. Mail left open over a weekend), run `python3 tracker.py --mark-away "2026-11-20 13:00" "2026-11-23 08:00"` in the project folder (local times), then reload the dashboard.
- If the footer says `restart tracker` after an update, stop the tracker and start it again.
