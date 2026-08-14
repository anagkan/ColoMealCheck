#!/usr/bin/env python3
"""PC/SC card reader bridge for kiosks whose reader cannot act as a keyboard.

The kiosk's normal input path is a keyboard-wedge reader: it types the card
value and presses Enter into a hidden input, and `kiosk.js` submits on Enter.
A PC/SC-only reader (the OMNIKEY 5x21 family, most CCID readers) presents no
HID keyboard to the operating system and can never do that.

This process closes that gap. It runs *on the kiosk laptop* — not in Docker,
which has no USB access at all — reads card serials over PC/SC, and offers them
on a WebSocket bound to loopback. The kiosk page connects to it and feeds each
serial into the same `submitScan()` it would have used for a tapped wedge scan,
so the result screen, the undo window and the offline queue all keep working
unchanged.

Why a local WebSocket rather than the two more obvious designs:

  * Synthesising keystrokes would need Accessibility permission on macOS and is
    blocked outright under Wayland — exactly the per-machine fragility we are
    trying to escape.
  * POSTing to /api/scan directly would bypass the kiosk UI: the member taps,
    the meal is recorded, and the screen in front of them never changes. It
    would also put an unauthenticated scan injector on the club LAN.

Binding to 127.0.0.1 is what makes it safe to run without authentication: the
socket is unreachable from the network. `--origin` narrows which pages may
connect, which matters because any site the kiosk browser visits could
otherwise open this socket and harvest card serials.

Run `--probe` first. If it lists no readers, no bridge in any language will
work, and the problem is the PC/SC driver rather than this script.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time

LOG = logging.getLogger("reader-bridge")

# PC/SC's reader-independent "give me the card serial" command. Contactless
# readers answer it with the UID/CSN without any card-specific knowledge, which
# is what keeps this script from caring whether the card is iCLASS, MIFARE or
# Seos.
GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]

# An iCLASS CSN is 8 bytes, which is what app/services/credentials.py validates
# at enrollment (16 hex characters). Other card families answer with 4 or 7.
# A mismatch is not fatal here — it is reported, and enrollment rejects it
# visibly — because silently padding or truncating a serial would bind cards to
# members under values that no reader will ever produce again.
EXPECTED_HEX_LEN = 16


def _import_pyscard():
    """Import pyscard with an error message that says what to install."""
    try:
        from smartcard.Exceptions import CardConnectionException, NoCardException
        from smartcard.System import readers

        return readers, NoCardException, CardConnectionException
    except ImportError:
        sys.exit(
            "pyscard is not installed.\n"
            "  pip install -r bridge/requirements.txt\n"
            "Linux also needs the PC/SC daemon and headers:\n"
            "  sudo apt install pcscd libpcsclite-dev swig\n"
            "macOS and Windows have a PC/SC stack built in."
        )


def read_uid(connection) -> str | None:
    """Return the card serial as uppercase hex, or None if the card refused."""
    data, sw1, sw2 = connection.transmit(GET_UID)
    # 6C XX means "wrong length, ask again for XX bytes" — some readers insist.
    if sw1 == 0x6C:
        data, sw1, sw2 = connection.transmit(GET_UID[:4] + [sw2])
    if (sw1, sw2) != (0x90, 0x00):
        LOG.warning("card did not return a serial (SW %02X%02X)", sw1, sw2)
        return None
    return "".join(f"{byte:02X}" for byte in data)


def poll_readers(emit, stop: threading.Event, interval: float) -> None:
    """Watch every attached reader and call `emit(value)` once per card tap.

    A card resting on the reader must not fire repeatedly, but a member tapping
    the same card twice is meaningful — it is how the kiosk opens the guest
    popup. So the rule is physical rather than time-based: a serial fires again
    only after the card has actually left the field.
    """
    readers, NoCardException, CardConnectionException = _import_pyscard()

    present: dict[str, str] = {}  # reader name -> serial currently on it
    known: list = []
    last_enumerated = 0.0

    while not stop.is_set():
        now = time.monotonic()
        # Re-enumerate periodically so unplugging and replugging the reader
        # recovers on its own. A kiosk that needs a restart after someone
        # knocks the cable out is a kiosk that dies on a Friday night.
        if now - last_enumerated > 3.0:
            last_enumerated = now
            try:
                found = list(readers())
            except Exception as err:  # pcscd down, service stopped, etc.
                LOG.warning("cannot list readers: %s", err)
                found = []
            names = {str(r) for r in found}
            if names != {str(r) for r in known}:
                if names:
                    LOG.info("readers: %s", ", ".join(sorted(names)) or "none")
                else:
                    LOG.warning("no PC/SC readers attached")
                for gone in set(present) - names:
                    present.pop(gone, None)
            known = found

        for reader in known:
            name = str(reader)
            try:
                connection = reader.createConnection()
                connection.connect()
            except NoCardException:
                present.pop(name, None)  # field is empty; arm the next tap
                continue
            except CardConnectionException as err:
                # A card mid-removal, or one that does not speak to this
                # reader. Treat as absent rather than crashing the loop.
                LOG.debug("connect failed on %s: %s", name, err)
                present.pop(name, None)
                continue
            except Exception as err:
                LOG.debug("unexpected connect error on %s: %s", name, err)
                continue

            try:
                serial = read_uid(connection)
            except Exception as err:
                LOG.debug("read failed on %s: %s", name, err)
                serial = None
            finally:
                try:
                    connection.disconnect()
                except Exception:
                    pass

            if serial is None:
                continue
            if present.get(name) == serial:
                continue  # still the same card sitting there
            present[name] = serial

            if len(serial) != EXPECTED_HEX_LEN:
                LOG.warning(
                    "serial %s is %d hex characters; enrollment expects %d. "
                    "Compare against a card enrolled with the old reader "
                    "before enrolling anyone with this one.",
                    serial,
                    len(serial),
                    EXPECTED_HEX_LEN,
                )
            LOG.info("card %s on %s", serial, name)
            emit(serial)

        stop.wait(interval)


def read_stdin(emit, stop: threading.Event, interval: float) -> None:
    """Serials typed at the terminal, standing in for taps.

    Everything downstream of the card read — the socket, the origin check, the
    kiosk's ownership rule, submitScan and the result screen — is exercised by
    this exactly as a real tap would exercise it. Only read_uid() and the
    polling loop go untested, which is the part that needs working hardware.
    """
    print("Simulating a reader. Type a card serial and press Enter (Ctrl-D to stop).")
    for line in sys.stdin:
        if stop.is_set():
            return
        value = line.strip().upper()
        if not value:
            continue
        if len(value) != EXPECTED_HEX_LEN:
            LOG.warning(
                "serial %s is %d hex characters; enrollment expects %d",
                value,
                len(value),
                EXPECTED_HEX_LEN,
            )
        LOG.info("simulated card %s", value)
        emit(value)
    stop.set()


async def run_server(
    host: str,
    port: int,
    origins,
    stop: threading.Event,
    interval: float,
    simulate: bool = False,
):
    import websockets

    # websockets reports a rejected handshake with a full traceback, which in a
    # kiosk log looks like a crash rather than the origin check doing its job.
    # Keep the one-line rejection, drop the stack.
    class _NoTraceback(logging.Filter):
        def filter(self, record):
            record.exc_info = None
            record.exc_text = None
            return True

    logging.getLogger("websockets.server").addFilter(_NoTraceback())

    loop = asyncio.get_running_loop()
    clients: set = set()

    async def handler(websocket, path=None):  # path dropped in websockets>=14
        clients.add(websocket)
        LOG.info("kiosk connected (%d total)", len(clients))
        try:
            await websocket.wait_closed()
        finally:
            clients.discard(websocket)
            LOG.info("kiosk disconnected (%d left)", len(clients))

    def emit(serial: str) -> None:
        """Called from the reader thread; hops onto the event loop."""
        # A serial read with no kiosk attached goes nowhere. Say so — the whole
        # point of this component is that a card tap must never vanish quietly,
        # and "I tapped and nothing happened" is unanswerable without this line.
        if not clients:
            LOG.warning(
                "no kiosk connected — %s went nowhere. Is the kiosk page open, "
                "reloaded since KIOSK_BRIDGE_URL was set, and served from the "
                "origin passed to --origin?",
                serial,
            )
            return
        message = json.dumps({"type": "scan", "value": serial})
        loop.call_soon_threadsafe(
            lambda: [asyncio.create_task(_send(c, message)) for c in list(clients)]
        )

    async def _send(client, message):
        try:
            await client.send(message)
        except Exception:
            clients.discard(client)

    # Bind before accepting input. Doing it the other way round prints the
    # "type a serial" prompt and *then* dies on a port clash, so the terminal
    # invites typing into a process that is already gone.
    async with websockets.serve(handler, host, port, origins=origins):
        LOG.info("bridge listening on ws://%s:%d", host, port)
        if origins is None:
            LOG.warning(
                "any page in this browser may connect; pass --origin "
                "http://<kiosk-host>:8000 to lock it down"
            )
        source = read_stdin if simulate else poll_readers
        thread = threading.Thread(
            target=source, args=(emit, stop, interval), daemon=True
        )
        thread.start()
        while not stop.is_set():
            await asyncio.sleep(0.5)


def probe(interval: float) -> int:
    """List readers and print serials. The first thing to run on a new machine."""
    readers, _, _ = _import_pyscard()
    try:
        found = list(readers())
    except Exception as err:
        print(f"Cannot reach the PC/SC service: {err}")
        return 1

    if not found:
        print(
            "No PC/SC readers found.\n\n"
            "The bridge cannot work until this list is non-empty. Check that\n"
            "the reader has a working driver for its USB interface class — a\n"
            "vendor-specific interface needs the manufacturer's driver, not\n"
            "the generic CCID one. On macOS compare against:\n"
            "  system_profiler SPSmartCardsDataType"
        )
        return 1

    print("Readers:", flush=True)
    for reader in found:
        print(f"  - {reader}", flush=True)
    print("\nTap a card (Ctrl-C to stop).", flush=True)

    stop = threading.Event()
    try:
        poll_readers(lambda serial: print(f"  card: {serial}", flush=True), stop, interval)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--origin",
        action="append",
        help="page origin allowed to connect, e.g. http://192.168.1.20:8000; "
        "repeatable. Omitted means any, which is only safe on a locked kiosk.",
    )
    parser.add_argument("--interval", type=float, default=0.25, help="poll seconds")
    parser.add_argument("--probe", action="store_true", help="list readers and exit")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="take serials from stdin instead of a reader, to test the kiosk "
        "wiring on a machine with no working PC/SC driver",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.probe:
        return probe(args.interval)

    stop = threading.Event()
    try:
        asyncio.run(
            run_server(
                args.host, args.port, args.origin, stop, args.interval, args.simulate
            )
        )
    except KeyboardInterrupt:
        stop.set()
    except OSError as err:
        # Overwhelmingly a second copy already running. A traceback here reads
        # as a crash in the reader code, which sends people looking in the
        # wrong place entirely.
        stop.set()
        LOG.error("cannot listen on %s:%d — %s", args.host, args.port, err)
        LOG.error(
            "another bridge is probably already running: "
            "lsof -nP -iTCP:%d -sTCP:LISTEN",
            args.port,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
