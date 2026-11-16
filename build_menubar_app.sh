#!/bin/zsh
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/build/DeskSignal.app"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

swiftc "$ROOT/DeskSignal.swift" \
  -o "$CONTENTS/MacOS/DeskSignal" \
  -framework AppKit \
  -framework Foundation

cp "$ROOT/DeskSignal-Info.plist" "$CONTENTS/Info.plist"
chmod +x "$CONTENTS/MacOS/DeskSignal"

echo "Built $APP"
echo "Open it with: open $APP"
