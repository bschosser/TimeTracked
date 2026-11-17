#!/bin/zsh
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/build/DeskSignal.app"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

# Universal binary (Apple Silicon + Intel) that runs on macOS 13 and newer,
# no matter which Mac or macOS version builds it.
MIN_MACOS="13.0"
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf "$BUILD_TMP"' EXIT
for ARCH in arm64 x86_64; do
  swiftc "$ROOT/DeskSignal.swift" \
    -O \
    -target "$ARCH-apple-macos$MIN_MACOS" \
    -o "$BUILD_TMP/DeskSignal-$ARCH" \
    -framework AppKit \
    -framework Foundation
done
lipo -create -output "$CONTENTS/MacOS/DeskSignal" "$BUILD_TMP/DeskSignal-arm64" "$BUILD_TMP/DeskSignal-x86_64"

cp "$ROOT/DeskSignal-Info.plist" "$CONTENTS/Info.plist"
chmod +x "$CONTENTS/MacOS/DeskSignal"
# Ad-hoc signature for the whole bundle (required for arm64 code; no developer account needed).
codesign --force --sign - "$APP"

echo "Built $APP"
echo "Open it with: open $APP"
