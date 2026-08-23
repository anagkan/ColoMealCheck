#!/usr/bin/env bash
#
# Launch the dining-room kiosk.
#
# The two Chrome flags below are not optional. Two features the kiosk needs
# exist only in a "secure context", and a plain-HTTP LAN address is not one:
# the webcam API (getUserMedia), without which enrollment photos fail silently,
# and service workers, without which the kiosk cannot start while the server is
# unreachable. --unsafely-treat-insecure-origin-as-secure marks this one origin
# trusted, and it is IGNORED unless --user-data-dir points at a separate
# profile. Both, or neither works.
#
# Usage:  ./run-kiosk.sh [server-host] [port]
#     e.g. ./run-kiosk.sh colonial-server.local 8000

set -euo pipefail

HOST="${1:-${COLOMEAL_HOST:-localhost}}"
PORT="${2:-${COLOMEAL_PORT:-8000}}"
ORIGIN="http://${HOST}:${PORT}"
# Under $HOME rather than $TMPDIR, and this matters more than it looks.
#
# The profile holds two things that have to outlive a reboot: the service
# worker's cached copy of the door screen, which is what lets the kiosk start
# when the server is away, and localStorage — where any scan taken during an
# outage is waiting to be replayed. On Linux /tmp is usually cleared on boot and
# on macOS $TMPDIR is purged after a few days idle, so a profile there loses
# both at exactly the moment they are needed: the power cut that took the server
# down took the cache with it.
PROFILE="${COLOMEAL_PROFILE:-$HOME/.colomealcheck-kiosk}"
LEGACY_PROFILE="${TMPDIR:-/tmp}/colomealcheck-kiosk"

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

# Earlier versions kept the profile in $TMPDIR. Move it rather than start
# empty: it may still hold scans that were taken offline and never replayed,
# and a fresh profile would silently abandon them.
if [[ ! -d "$PROFILE" && -d "$LEGACY_PROFILE" ]]; then
  echo "Moving the kiosk profile out of the temp directory, so its offline"
  echo "cache and any queued scans survive a reboot."
  mv "$LEGACY_PROFILE" "$PROFILE"
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
