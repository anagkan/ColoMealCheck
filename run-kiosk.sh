#!/usr/bin/env bash
#
# Launch the dining-room kiosk.
#
# The two Chrome flags below are not optional. The webcam API (getUserMedia)
# only exists in a "secure context", and a plain-HTTP LAN address is not one —
# without these, navigator.mediaDevices is undefined and enrollment photos fail
# silently. --unsafely-treat-insecure-origin-as-secure marks this one origin
# trusted, and it is IGNORED unless --user-data-dir points at a separate
# profile. Both, or neither works.
#
# Usage:  ./run-kiosk.sh [server-host] [port]
#     e.g. ./run-kiosk.sh colonial-server.local 8000

set -euo pipefail

HOST="${1:-${COLOMEAL_HOST:-localhost}}"
PORT="${2:-${COLOMEAL_PORT:-8000}}"
ORIGIN="http://${HOST}:${PORT}"
PROFILE="${TMPDIR:-/tmp}/colomealcheck-kiosk"

find_chrome() {
  local candidates=(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "/Applications/Chromium.app/Contents/MacOS/Chromium"
    "$(command -v google-chrome || true)"
    "$(command -v google-chrome-stable || true)"
    "$(command -v chromium || true)"
    "$(command -v chromium-browser || true)"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  return 1
}

CHROME="$(find_chrome)" || {
  echo "Could not find Chrome or Chromium. Install one, or set CHROME=/path/to/chrome." >&2
  exit 1
}

echo "Checking ${ORIGIN}/healthz ..."
if ! curl -fsS --max-time 5 "${ORIGIN}/healthz" >/dev/null; then
  echo "WARNING: ${ORIGIN} is not answering. Starting anyway — the kiosk will" >&2
  echo "         queue scans locally and sync them once the server is back." >&2
fi

mkdir -p "$PROFILE"

exec "$CHROME" \
  --kiosk \
  --user-data-dir="$PROFILE" \
  --unsafely-treat-insecure-origin-as-secure="$ORIGIN" \
  --autoplay-policy=no-user-gesture-required \
  --disable-features=TranslateUI \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --no-first-run \
  --noerrdialogs \
  "$ORIGIN/"
