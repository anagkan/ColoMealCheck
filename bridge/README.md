# PC/SC reader bridge

For kiosks whose card reader **cannot act as a keyboard**.

The kiosk's normal input path is a keyboard-wedge reader (the OMNIKEY 5427CK in
the main README): it types the card serial and presses Enter, and `kiosk.js`
submits on Enter. A PC/SC-only reader — the OMNIKEY 5x21 family, and most CCID
readers — presents no HID keyboard to the operating system at all, so nothing is
ever typed and the kiosk sees nothing. No amount of app configuration changes
that.

This bridge reads serials over PC/SC and offers them to the kiosk page on a
loopback WebSocket. It runs **on the kiosk laptop**, never in Docker — the
container has no USB access.

If you are using a wedge reader, ignore this directory. Leaving
`KIOSK_BRIDGE_URL` unset makes every part of it inert.

## Does your reader work at all?

Do this before anything else, on the machine with the reader plugged in.

```bash
python3 -m venv bridge/.venv
bridge/.venv/bin/pip install -r bridge/requirements.txt
bridge/.venv/bin/python bridge/reader_bridge.py --probe
```

`--probe` lists PC/SC readers and prints the serial of every card you tap.

**If the reader list is empty, stop here.** No bridge in any language can read a
reader the PC/SC stack cannot see, and the rest of this document will not help.
That is a driver problem:

- **macOS** — compare against `system_profiler SPSmartCardsDataType`. A reader
  whose USB interface class is vendor-specific (`bInterfaceClass = 255`, visible
  via `ioreg -r -c IOUSBHostInterface -l`) needs the manufacturer's own driver;
  the built-in `ifd-ccid` will not bind to it. Check that any vendor bundle in
  `/usr/local/libexec/SmartCardServices/drivers/` actually loads — one listed as
  `(null):(null)` is failing. Confirm it has a slice for this machine's
  architecture before believing it:
  `file <bundle>/Contents/MacOS/*.dylib`. An x86_64-only driver cannot load into
  the arm64 PC/SC daemon on Apple Silicon, and Rosetta does not help. That is
  what rules out the OMNIKEY 5x21 family here: HID's macOS driver for it is
  x86_64/i386 only, so those readers cannot be used on an Apple Silicon Mac at
  all — by this bridge or by anything else.

A reader presenting a standard CCID interface (`bInterfaceClass = 11`) needs no
vendor driver on any platform. The OMNIKEY 5427CK ships that way, and macOS
drives it with the built-in `ifd-ccid` out of the box.
- **Linux** — `sudo apt install pcscd libpcsclite-dev swig`, then confirm with
  `pcsc_scan`.
- **Windows** — the PC/SC stack is built in; install the vendor driver and
  confirm the reader appears in Device Manager under *Smart card readers*.

## Check the serial format before enrolling anyone

`app/services/credentials.py` validates a CSN as **16 hex characters** at
enrollment — an 8-byte iCLASS CSN. If `--probe` prints something shorter, this
reader disagrees with the one your existing cards were enrolled with, and the
bridge warns on every scan rather than padding it. Padding would bind members to
values no reader will ever produce again.

Tap a card that is **already enrolled** and confirm the serial matches what is on
file before switching a live club over. If it does not, existing enrollments do
not survive the hardware change, and that is a data migration rather than a
config change.

## Testing the wiring without a working reader

`--simulate` takes serials from stdin instead of from a card, so you can prove
out everything downstream of the card read before the driver works:

```bash
bridge/.venv/bin/python bridge/reader_bridge.py --simulate \
  --origin http://localhost:8000
```

Type a serial, press Enter, and watch the kiosk. That exercises the socket, the
origin check, the ownership rule, `submitScan()` and the result screen — the
whole path a tap takes, short of the card read itself.

Use the serial of a card that is **already enrolled** to see a real check-in.

The full path, card included, has been run against an OMNIKEY 5427CK in CCID
mode on macOS: tapping filled and validated the card field on `/enroll`, and a
tap at the kiosk checked a member in.

## Running it

```bash
bridge/.venv/bin/python bridge/reader_bridge.py --origin http://192.168.1.20:8000
```

Use the origin the kiosk browser actually loads — scheme, host and port, no
trailing slash. Without `--origin` any page in that browser may open the socket
and read card serials; it starts anyway, with a warning, so you are not blocked
while testing.

Then point the app at it and restart the container:

```
KIOSK_BRIDGE_URL=ws://127.0.0.1:8765
```

`127.0.0.1` is deliberate — it is the kiosk laptop's own loopback, resolved by
the browser, not an address on the server. That is also what makes it safe to
run unauthenticated: the socket is unreachable from the network.

The kiosk shows **Reader bridge offline** whenever the socket is down, and
reconnects with backoff. A reader that stops working silently is the failure
mode this whole design is trying to avoid.

## What it does and does not change

The bridge is an input path, not a second scanning path. It hands the serial to
the same `submitScan()` a tapped wedge scan uses, so the result screen, the undo
window, the duplicate-tap-opens-the-guest-popup behaviour and the offline queue
all work unchanged. It obeys the same ownership rule as the hidden sink — no
submission while a popup is open or on a screen with something to type into.

On `/enroll` it fills the card box and validates it, but never submits.
Enrollment is staff work, and a card tapped by mistake must be correctable
rather than already bound.

Two things genuinely differ from a wedge:

- **A second process can die.** A wedge reader has nothing to crash. Run this
  under launchd, systemd or Task Scheduler so it restarts with the machine.
- **The browser must reach loopback.** Fine over plain HTTP, which is how the
  kiosk runs today. If you later put TLS in front of the app, a `wss://`
  connection to localhost needs a certificate the browser trusts, which is
  awkward — worth knowing before you plan that migration.
