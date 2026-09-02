#!/usr/bin/env bash
#
# Launch the dining-room kiosk.
#
# Two features the kiosk needs exist only in a "secure context": the webcam API
# (getUserMedia), without which enrollment photos fail silently, and service
# workers, without which the kiosk cannot start while the server is unreachable.
#
# An https:// origin is a secure context on its own merits — that is the
# deployment in DEPLOYMENT.md, where `tailscale serve` terminates TLS on a real
# certificate. A plain-HTTP LAN address is not, and there
# --unsafely-treat-insecure-origin-as-secure has to stand in, marking the one
# origin trusted. That flag is IGNORED unless --user-data-dir points at a
# separate profile, so on HTTP it is both flags or neither. This script adds it
# only when the origin actually needs it.
#
# --user-data-dir is passed either way, for the reason under PROFILE below.
#
# Usage:  ./run-kiosk.sh <origin>
#         ./run-kiosk.sh [server-host] [port]
#     e.g. ./run-kiosk.sh https://colonial-server.<tailnet>.ts.net
#          ./run-kiosk.sh colonial-server.local 8000

set -euo pipefail

# Anything carrying a scheme is taken verbatim, port and all — that is how the
# Tailscale deployment names a server that answers on 443 and so has no port to
# quote. Anything else is a bare hostname and gets the old http://host:port
# treatment, so the LAN invocation keeps working unchanged.
if [[ -n "${COLOMEAL_ORIGIN:-}" ]]; then
  ORIGIN="${COLOMEAL_ORIGIN}"
elif [[ "${1:-}" == http://* || "${1:-}" == https://* ]]; then
  ORIGIN="$1"
else
  HOST="${1:-${COLOMEAL_HOST:-localhost}}"
  PORT="${2:-${COLOMEAL_PORT:-8000}}"
  ORIGIN="http://${HOST}:${PORT}"
fi

# Chrome matches --unsafely-treat-insecure-origin-as-secure against an origin
# with no trailing slash, and a stray one would double up in the URLs below.
ORIGIN="${ORIGIN%/}"

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

CHROME_ARGS=(
  --kiosk
  --user-data-dir="$PROFILE"
  --autoplay-policy=no-user-gesture-required
  --disable-features=TranslateUI
  --disable-session-crashed-bubble
  --disable-infobars
  --no-first-run
  --noerrdialogs
)

# Only plain HTTP needs the exception. Passing it for an https:// origin would
# be worse than redundant: it tells Chrome to trust the origin whatever the
# certificate does, which is exactly how you fail to notice the morning the
# certificate stopped renewing.
if [[ "$ORIGIN" == https://* ]]; then
  echo "Secure origin — the webcam and service worker need no exception."
else
  echo "Insecure origin — marking ${ORIGIN} trusted so the webcam and service worker work."
  CHROME_ARGS+=(--unsafely-treat-insecure-origin-as-secure="$ORIGIN")
fi

exec "$CHROME" "${CHROME_ARGS[@]}" "$ORIGIN/"
