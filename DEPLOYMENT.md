# Deployment

How ColoMealCheck goes from a laptop to the dining room door.

The two halves of the system are not on the same network and cannot be put on
one. The Docker stack runs on a server; the kiosk is an old Chromebook sitting
by the door on whatever network reaches that room. Tailscale joins them into a
single private network, and — this is the part that makes the rest work —
gives the server a real HTTPS name that the Chromebook can open.

```
       server (anywhere)                         dining room
  ┌───────────────────────────┐           ┌──────────────────────┐
  │  docker compose           │           │  Chromebook          │
  │    api  →  127.0.0.1:8000 │           │    Chrome (PWA)      │
  │    db   →  pgdata volume  │           │    OMNIKEY reader    │
  │                           │           │                      │
  │  tailscaled               │           │  tailscaled          │
  │  tailscale serve :443 ────┼── WireGuard ──┐                  │
  └───────────────────────────┘           └───┼──────────────────┘
                                              │
                    https://colonial-server.<tailnet>.ts.net
```

Nothing is exposed to the public internet. `tailscale serve` publishes only to
the tailnet; the container's port is bound to the server's loopback so even the
server's own LAN cannot reach it.

---

## Why HTTPS, and why this shapes everything below

The kiosk needs two browser features that only exist in a **secure context**:
`getUserMedia` for enrollment photos, and service workers for starting up when
the server is away. On a plain `http://host:8000` address neither is available.

On a normal laptop `run-kiosk.sh` works around that with
`--unsafely-treat-insecure-origin-as-secure`. **On a Chromebook that escape
hatch is gone** — ChromeOS gives you no way to pass Chrome command-line flags
without putting the device in developer mode, and `run-kiosk.sh` cannot run on
ChromeOS at all.

So the Chromebook deployment is not "the LAN setup, over Tailscale". It needs
real HTTPS, and Tailscale supplies it: with MagicDNS and HTTPS certificates
enabled, `tailscale serve` terminates TLS on a genuine Let's Encrypt
certificate for `<machine>.<tailnet>.ts.net` and proxies to the container. The
browser sees a proper `https://` origin, both features light up, and no flags
are needed.

This is a strict improvement over the LAN setup, not a workaround for it.

### Pick the origin once, on day one

The offline queue and the service-worker cache live in browser storage, which
is **keyed to the exact origin**. `https://colonial-server.tailnet.ts.net` and
`http://100.x.y.z:8000` are two different, separate, empty stores. If the kiosk
is ever opened at the IP and a scan is queued there, that scan will not sync
once you move to the hostname — it will sit invisibly in the other store.

Decide the Tailscale hostname before the first real service and never change
it. Everything below assumes `colonial-server`.

---

## Part 1 — The server

Any always-on Linux box works: a mini-PC in the club office, a VPS, a spare
desktop. It needs Docker, and it needs to be on and reachable at meal times.

### 1.1 Install Docker and bring the stack up

```bash
git clone https://github.com/anagkan/ColoMealCheck.git
cd ColoMealCheck
cp .env.example .env
```

Edit `.env`. Three values matter before the first boot:

```bash
SECRET_KEY=          # python3 -c "import secrets; print(secrets.token_urlsafe(48))"
POSTGRES_PASSWORD=   # anything long; it never leaves the compose network
STAFF_PIN=           # typed at the door for enrollment and overrides
```

And one more, which is the difference between a server and an open door:

```bash
# Bind the container's port to loopback only. Tailscale reaches it there;
# nothing on the server's own LAN can.
API_PORT=127.0.0.1:8000
```

Compose expands `${API_PORT}:8000` into `127.0.0.1:8000:8000`, which is exactly
the binding wanted. If that trick ever stops working on a future Compose
version, edit the `ports:` line in the compose file directly — the intent is
what matters, not the mechanism. Verify it took with `docker compose ps`: the
port column must read `127.0.0.1:8000->8000/tcp`, not `0.0.0.0:8000->8000/tcp`.

Then:

```bash
docker compose up -d          # or -f docker-compose.deploy.yml, see below
docker compose logs api       # the admin password prints here if generated
```

Set `ADMIN_PASSWORD` in `.env` and that is the password, re-applied on every
boot — to change it later, edit `.env` and `docker compose up -d` again. There
is no screen in the app that changes an admin password, so this is the only way.

Left blank, the first boot generates one and prints it to the log **once**; copy
it out now, because nothing shows it again. Either way `.env` is the file to
guard: anyone who can read it can sign in as an admin.

### 1.2 Build here, or pull the published image

`docker-compose.yml` builds from source. `docker-compose.deploy.yml` is
identical except that `api` pulls `ghcr.io/anagkan/colomealcheck`, so it runs on
a server with no copy of the source. Both read the same `.env` and are
interchangeable.

The published image is private; pulling it needs `docker login ghcr.io` with a
GitHub token carrying `read:packages`.

**Pin the tag before the club depends on this.** The deploy file points at
`:latest`, which CI rewrites on every merge to main — a restart at the wrong
moment moves the dining room onto a build nobody asked for. Every CI build also
publishes the bare short commit SHA as a tag (e.g. `:ca55f6f`), and those are
never rewritten. Pin to one.

Remember that the app runs from the container: after any code change, rebuild
and `up -d` before expecting to see it.

### 1.3 Join the tailnet

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=colonial-server --advertise-tags=tag:colomeal-server
```

Two things to do in the [admin console](https://login.tailscale.com/admin)
right after:

- **Disable key expiry** for this machine (Machines → ⋯ → Disable key expiry).
  Default expiry is 180 days, and when it lapses the kiosk simply stops
  reaching the server, in the middle of a meal, with no warning at the door.
  Devices authenticated with a tag do not expire, but set it explicitly anyway.
- **Enable MagicDNS and HTTPS Certificates** (DNS page). Both are required for
  the next step. Note the tailnet name shown there — something like
  `tailfe8c.ts.net`. The server's full name is
  `colonial-server.tailfe8c.ts.net`.

Tags need declaring in the ACL file before `--advertise-tags` will be accepted;
see §4.

### 1.4 Put TLS in front of the container

```bash
sudo tailscale serve --bg 8000
tailscale serve status
```

That proxies `https://colonial-server.<tailnet>.ts.net/` → `http://127.0.0.1:8000`,
fetching and renewing the certificate itself. The `--bg` config persists across
reboots. On older Tailscale versions the syntax is
`tailscale serve https / proxy 8000` — check `tailscale serve --help` if the
short form is rejected.

Uvicorn already runs with `--forwarded-allow-ips '*'`, so it reads the
`X-Forwarded-Proto` header Tailscale sets and generates correct absolute URLs.

**Do not use `tailscale funnel`.** Funnel is the same mechanism aimed at the
public internet. This app has no login on the check-in path by design — a queue
at dinner is no place for one — and it holds student PUIDs and photographs.
`serve` keeps it tailnet-only. Funnel would put it on the open web.

Check it from the server:

```bash
curl -fsS https://colonial-server.<tailnet>.ts.net/healthz
# {"ok":true,"database":"up"}
```

---

## Part 2 — The Chromebook

### 2.1 What an old Chromebook can and cannot do

Check three things before committing to the device:

1. **Play Store support.** Tailscale ships an Android app, and that is the
   supported way onto ChromeOS. No Play Store, no Tailscale.
2. **Auto-update expiration (AUE).** Settings → About ChromeOS → Additional
   details. A Chromebook past its AUE date gets no more Chrome updates — it
   will keep working, but it stops receiving security fixes, and eventually
   some part of the web platform the kiosk uses will age out from under it.
   Fine for a season; not something to build on for years.
3. **A real signed-in profile, not Guest mode.** Guest sessions are wiped at
   sign-out — including the service-worker cache and the offline queue. That
   destroys precisely the two things that let the kiosk survive an outage. Use
   a dedicated Google account for the club and stay signed into it.

If the device fails (1) or (2), there are two ways out. The cheaper one is a
small Linux mini-PC or a Raspberry Pi running `run-kiosk.sh` against the same
`https://` address — everything on the server side is unchanged. The other is
to stop treating the Chromebook as a Chromebook: replace its firmware and put
Debian on it, which lands you in exactly that same supported case, on hardware
you already own. That is §2.6, and for an x86_64 Chromebook it is the better
answer — it also puts the device back on a browser that still gets security
updates, which §2.1's second check is really about.

### 2.2 Install Tailscale

Install the Tailscale Android app from the Play Store, sign in to the same
tailnet, and accept the VPN prompt.

**Then verify that ChromeOS routes the browser through it, not just Android
apps.** Recent ChromeOS versions apply an Android VPN to the whole device, but
this is the one assumption in this document worth testing rather than
believing. Open Chrome on the Chromebook and load:

```
https://colonial-server.<tailnet>.ts.net/healthz
```

If you get `{"ok":true,...}`, ChromeOS is routing browser traffic over the
tailnet and you are done. If the page does not load while the Tailscale app
says it is connected, the VPN is only covering Android apps — stop and choose
the mini-PC fallback rather than fighting it.

In the admin console, disable key expiry on the Chromebook too, and tag it
`tag:colomeal-kiosk`.

### 2.3 Install the kiosk as an app

The app ships a web manifest with `display: fullscreen`, so Chrome will install
it as a PWA that opens without browser chrome — the closest thing to kiosk mode
available without enterprise enrollment.

1. Open `https://colonial-server.<tailnet>.ts.net/` in Chrome.
2. Menu → Cast, save and share → **Install page as app** (wording varies by
   version; look for Install).
3. Pin the installed app to the shelf.
4. Launch it once from the shelf and confirm it opens fullscreen with no
   address bar.

Then, on that first successful load, grant camera permission when prompted —
enrollment photos need it, and the permission is remembered per origin.

### 2.4 Settle the device down

- **Power:** Settings → Device → Power → "Keep display on while charging", and
  set sleep to never while plugged in. A sleeping Chromebook is a closed door.
- **Reader:** the OMNIKEY 5427CK in keyboard-wedge mode is an ordinary USB
  keyboard and needs no driver on ChromeOS. Before opening the app, tap a card
  into any text field and confirm 16 hex digits appear followed by a newline.
  Reader problems surface there, not in the app.
- **The PC/SC bridge is not available here.** `bridge/reader_bridge.py` needs a
  local Python process, which ChromeOS will not give you. If the reader turns
  out to be PC/SC-only and cannot type, the Chromebook cannot be the kiosk.
  Leave `KIOSK_BRIDGE_URL` empty.
- **Reader reconfiguration still needs Windows.** The reader's own web tool is
  served over a CDC-EEM interface that only Windows binds. Borrow a Windows
  machine if the reader is ever reset or replaced.

### 2.5 Confirm the offline path actually works

This is worth doing once, deliberately, before the first real meal — it is the
whole reason the secure context matters.

1. Load the kiosk normally and let it sit for a few seconds so the service
   worker installs its cache.
2. Stop the server: `docker compose stop api`.
3. Reload the kiosk. It should still open, with the banner reading
   **Offline — meal times unavailable**.
4. Tap a card. It should say "Saved offline", with no name or photo — the
   server is what turns a card into a person.
5. `docker compose start api`. Within about 20 seconds the queued scan should
   sync and appear in `/admin`.

If step 3 shows Chrome's dinosaur instead, the service worker never installed —
which almost always means the page was opened at an `http://` address, or in
Guest mode.

### 2.6 Replacing ChromeOS with Linux on an out-of-support Chromebook

Written against the **Acer Chromebook 11 CB3-131** (board name `gnawty`, Intel
Celeron N2840, Bay Trail, x86_64), which fails both of §2.1's first two checks:
it never got the Play Store, so there is no way to run Tailscale under
ChromeOS, and it is years past its auto-update date. Flashing it is not a
workaround for those failures — it removes the thing that was failing.

The same procedure covers any x86_64 Chromebook on MrChromebox's supported
list. **It does not cover ARM Chromebooks** (a Rockchip or MediaTek board — the
Asus C201P, for instance): that firmware is x86-only, and those devices need a
different and much fiddlier route.

At the end of this the machine is an ordinary little Debian laptop,
`run-kiosk.sh` runs on it unmodified, and §2.2–§2.5 are replaced by §2.6.6
below.

#### What this costs you, before you start

**ChromeOS will be gone.** A Full ROM flash overwrites the stock firmware
entirely; the device stops being a Chromebook and starts being a PC. The way
back is the firmware backup taken in §2.6.2, and *there is no other way back* —
a lost backup means a device that can never run ChromeOS again. Take it, and
put the USB stick somewhere the club will still have it in two years.

**A failed flash can brick the device.** The risk is small and well-trodden,
but it is real, and recovery needs a hardware programmer. Do not flash a
Chromebook you cannot afford to lose, do not flash on battery alone, and do not
interrupt it.

You need: a Phillips #00 screwdriver, a plastic spudger or guitar pick, a USB
stick for the firmware backup, a second USB stick (8 GB+) for the Debian
installer, mains power, and about two hours.

#### 2.6.1 Enter Developer Mode

This wipes local ChromeOS state, so do it before anything you would miss.

1. Hold **Esc + Refresh (F3)** and press **Power**. The recovery screen appears.
2. Press **Ctrl+D**, then **Enter** to confirm.
3. Wait for the transition — about ten minutes, with a reboot in the middle.

From now on every boot shows the "OS verification is OFF" screen for 30
seconds; **Ctrl+D** skips it. This is temporary — the Full ROM flash removes
that screen along with the rest of the stock firmware.

Do **not** press Space at that screen. Space re-enables verification and drops
you back out of Developer Mode, wiping the device again.

#### 2.6.2 Back up the stock firmware

Do this *before* opening the case: the backup does not need write protection
disabled, and taking it first means the irreversible step comes last.

Get a root shell. On ChromeOS R117 and later this has to be a VT2 terminal
rather than the crosh shell — and while a device this old is on something much
earlier, VT2 works on every version, so use it either way:

```
[Ctrl+Alt+F2]      # F2 is the → key on a Chromebook
login: chronos     # no password
```

Insert the backup USB stick, then run the firmware utility script. **Run it as
`chronos`, not as root** — the script says so itself, and running it under `sudo
su` breaks it:

```bash
cd; curl -LOf https://mrchromebox.tech/firmware-util.sh && sudo bash firmware-util.sh
```

Choose **Backup Stock Firmware**, write it to the USB stick, and confirm the
file is there. Label the stick with the device's serial number. That file is
the only route back to ChromeOS.

#### 2.6.3 Remove the write-protect screw

Bay Trail devices gate firmware writes on a physical screw that completes a
ground connection; removing it leaves the pin floating and write protection
off. Full ROM flashing requires this. (RW_LEGACY does not, but RW_LEGACY leaves
ChromeOS in place and is not what we want here.)

1. Power off and **unplug the charger**.
2. Remove the bottom case — perimeter screws, some under the rubber feet, then
   work the clips with the spudger.
3. Find the WP screw on the mainboard. MrChromebox's supported-devices page has
   a photo for `gnawty`; match it rather than guessing, because it looks like
   every other screw on the board.
4. Remove it. It does not go back — keep it in the bag with the backup stick.
5. Reassemble.

#### 2.6.4 Flash the UEFI Full ROM

Boot, drop to VT2 and log in as `chronos` again, plug the charger in, and rerun
the same script:

```bash
cd; curl -LOf https://mrchromebox.tech/firmware-util.sh && sudo bash firmware-util.sh
```

Choose **Install/Update UEFI (Full ROM) Firmware**. It will confirm that write
protection is disabled — if it says otherwise, stop and revisit §2.6.3; do not
force it. Accept the offer to back up the stock firmware if you skipped §2.6.2.

Then leave it alone until it finishes. It takes a couple of minutes.

Reboot into a normal UEFI firmware screen. The Chromebook is now a PC.

#### 2.6.5 Install Debian

Write a **Debian 13 (trixie) amd64** netinst image to the second USB stick,
plug it in, and boot it (the boot menu is on **Esc** or **F7**, depending on
where the firmware landed).

Three things worth knowing during the install:

- The internal 16 GB eMMC appears as `/dev/mmcblk0`, not `/dev/sda`. Use the
  whole disk. If the installer does not see it at all, the USB stick is
  probably enumerating first — check you are not looking at it instead.
- Debian 13's installer ships non-free firmware by default, so the Wi-Fi works
  during installation. Leave that enabled.
- Choose a **minimal** install: untick every desktop environment, keep only
  "standard system utilities" and SSH server. The kiosk needs a browser and an
  X server, not GNOME — 2 GB of RAM does not have a desktop's worth to spare.
  Create a user called `kiosk`.

Then, from an SSH session or the console:

```bash
sudo apt update && sudo apt install -y \
  chromium xserver-xorg xinit x11-xserver-utils matchbox-window-manager \
  unclutter curl git zram-tools
```

`run-kiosk.sh` finds Debian's `chromium` binary by name — nothing to configure.

**On 2 GB of RAM.** The common CB3-131 has 2 GB, and that is enough for this
kiosk — but only on the minimal install above. Debian with no desktop idles
around 200 MB; X plus Chromium showing one page is 500–700 MB; `tailscaled` is
another 50. Call it a gigabyte in steady state, with a gigabyte spare. Install
GNOME or Xfce on this machine and that headroom is gone before the browser
opens, which is the real reason §2.6.5 says untick every desktop environment.

`zram-tools` is in the list for the same reason. 2 GB deserves a safety net,
but a swapfile on a decade-old 16 GB eMMC is slow and wears the flash; zram
puts compressed swap in RAM instead, costing no writes at all. The defaults are
fine — check it took with `zramctl` after a reboot.

The page itself does not grow: the offline queue is capped at 500 entries and
kept in `localStorage` rather than the heap, member photos reuse a single
`<img>`, and every list is rebuilt rather than appended to. What does grow is
Chromium, slowly, over weeks — see the nightly restart in §2.6.6.

#### 2.6.6 Bring the kiosk up

This replaces §2.2–§2.4 for a converted device.

**Join the tailnet:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=colonial-kiosk --advertise-tags=tag:colomeal-kiosk
sudo tailscale set --auto-update      # keeps the client current unattended
curl -fsS https://colonial-server.<tailnet>.ts.net/healthz
```

Disable key expiry for the kiosk in the admin console, exactly as §1.3 does for
the server. An expired key at the door is §4's "will not come back on its own"
row.

**Get the launcher** — the whole repo, or just the one script:

```bash
git clone https://github.com/anagkan/ColoMealCheck.git ~/ColoMealCheck
```

**Start X and the kiosk on boot.** Auto-login on tty1:

```bash
sudo systemctl edit getty@tty1
```

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin kiosk --noclear %I $TERM
```

`~/.bash_profile`, so logging in on tty1 starts X:

```bash
if [[ -z $DISPLAY && $XDG_VTNR -eq 1 ]]; then
  exec startx
fi
```

`~/.xinitrc` — blanking off, cursor hidden, a window manager, and the kiosk in
a loop so a browser crash at 6pm re-opens the door instead of leaving a black
screen. This file *is* the session: there is no session manager, and X exits
when the last command here does, which is what the loop prevents.

```bash
xset s off -dpms
xset s noblank
unclutter -idle 3 &
matchbox-window-manager -use_titlebar no &

while :; do
  "$HOME/ColoMealCheck/run-kiosk.sh" https://colonial-server.<tailnet>.ts.net
  sleep 2
done
```

**Why a window manager at all**, when the whole point of §2.6.5 was to install
no desktop: a window manager is not a desktop environment. Matchbox is under
half a megabyte, exists specifically for kiosks, and earns its place twice
over. It honours `--kiosk`'s fullscreen request, which with no WM running is a
request to nobody — Chromium can end up mapping a window that is not quite the
screen. And it assigns keyboard focus.

That second one is what matters here, because **the card reader is a
keyboard**. With no WM, X falls back to `PointerRoot` focus: keystrokes go to
whatever window the pointer happens to be over. A single fullscreen window
makes that work right up until it doesn't, and the failure is silent — cards
tap, nothing appears, and the app is blameless. `kiosk.js` re-focuses the input
box every 1.5s, but that only moves focus *within* a page that already has it.

Do not read this as licence to install a desktop later. Matchbox draws nothing
and starts no daemons; GNOME would take the 2 GB budget in §2.6.5 apart.

The `https://` origin is the point of all this: `run-kiosk.sh` sees a secure
origin, skips `--unsafely-treat-insecure-origin-as-secure`, and the webcam and
service worker work on their own merits. Pass the Tailscale name, never the
`100.x` address — §"Pick the origin once" explains what happens if the two get
mixed.

**Restart the browser nightly.** Chromium's memory creeps upward over weeks of
uptime no matter how well-behaved the page is, and on 2 GB that is worth
pre-empting rather than discovering at dinner. The `while :;` loop above makes
it free — kill the browser and the loop reopens the kiosk within seconds:

```bash
sudo tee /etc/cron.d/kiosk-restart <<'EOF'
# 04:00, well clear of breakfast. The .xinitrc loop reopens it immediately.
0 4 * * * kiosk /usr/bin/pkill -u kiosk chromium
EOF
```

Anything queued offline survives this: the queue is in `localStorage`, which is
on disk in the profile that §"PROFILE" in `run-kiosk.sh` deliberately keeps
under `$HOME`.

**Shut the console away.** By default `[Ctrl+Alt+F2]` leaves the kiosk and lands
on a text console already logged in as `kiosk` — fine while you are building
the thing, wrong in a dining room. Close both that and the kill-X chord:

```bash
sudo tee /etc/X11/xorg.conf.d/10-kiosk.conf <<'EOF'
Section "ServerFlags"
    Option "DontVTSwitch" "on"
    Option "DontZap"      "on"
EndSection
EOF
```

Then take sudo away from the account that is permanently logged in at the door.
Make yourself a separate admin user for SSH first, confirm you can reach it and
escalate, and only then:

```bash
sudo adduser <you>
sudo adduser <you> sudo
# from a *second* session, having verified the new account works:
sudo deluser kiosk sudo
```

This is the same reasoning as §3's tailnet ACL, one layer down: the machine
standing unattended by a door should be able to show a web page and nothing
else. A student who finds a keyboard shortcut then finds a web page.

**Settle the hardware down:**

- Lid: `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`, so a closed lid
  is not a closed door. Reboot after editing.
- Sleep, belt and braces:
  `sudo systemctl mask sleep.target suspend.target hibernate.target`.
- Reader: the OMNIKEY in keyboard-wedge mode is a USB HID keyboard and needs no
  driver. Tap a card into a text editor first and confirm 16 hex digits and a
  newline. Reader problems surface there, not in the app.
- Unlike ChromeOS, this device *can* run `bridge/reader_bridge.py` if the reader
  ever ends up PC/SC-only — §2.4's restriction does not apply here.
- Sound is not needed at all: the kiosk makes no noise, so Bay Trail's awkward
  Linux audio never comes up.
- Webcam: confirm it works before relying on enrollment photos. It is a USB
  UVC device and should just appear, but check rather than assume.

Then run §2.5's offline test, unchanged. It matters as much here as anywhere.

#### 2.6.7 What the staff need to know

The whole point of the chain above is that this list is short. Print it and
tape it inside a cupboard by the door.

- **To start it:** press the power button. Wait half a minute. That is all —
  there is nothing to log into and nothing to open.
- **If the screen is black:** press a key first; if still black, hold power for
  ten seconds, then press it again.
- **If it says "Offline":** keep tapping cards. Scans are being saved and will
  file themselves when the server comes back. This is working as designed, not
  a fault to report.
- **If a red "scans not recorded" badge appears:** it will not clear itself.
  Tell whoever runs the system — those are meals with no row behind them, and
  §4 explains how they get entered by hand.
- **Never close the lid or unplug it.** A closed door is a closed door.
- **Do not "fix" it by pressing keys.** Whatever gets closed, the kiosk reopens
  itself within a few seconds. Wait before intervening.

#### If you need ChromeOS back

Boot from the Debian stick into a live shell, or reinstall enough of anything
to get a terminal, run the same firmware utility script, and choose **Restore
Stock Firmware** with the §2.6.2 backup on a USB stick. Then build a ChromeOS
recovery stick with the Chromebook Recovery Utility and reinstall. Without that
backup file, this paragraph does not work.

---

## Part 3 — Locking the tailnet down

Tailscale's default ACL lets every device talk to every other device. Narrow it
so the kiosk can reach the server and nothing else. In the admin console's
Access Controls:

```jsonc
{
  "tagOwners": {
    "tag:colomeal-server": ["autogroup:admin"],
    "tag:colomeal-kiosk":  ["autogroup:admin"],
  },
  "acls": [
    // The door screen talks to the app over HTTPS. Nothing else.
    {
      "action": "accept",
      "src":    ["tag:colomeal-kiosk"],
      "dst":    ["tag:colomeal-server:443"],
    },
    // Officers' own laptops reach the admin screens.
    {
      "action": "accept",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:colomeal-server:443"],
    },
  ],
}
```

The effect: a compromised or stolen Chromebook is a device that can open one
HTTPS port on one machine. It cannot reach the Postgres port (which is not
published outside the compose network anyway), the server's SSH, or anything
else on the tailnet.

Postgres deserves a note: it is never published to a host port at all — only
the `api` container reaches it, over the compose network. Nothing in this
deployment should change that.

### If the kiosk is lost or stolen

§1.3 disables key expiry on purpose — a key that lapses mid-meal is a dead door
— but the cost of that choice is that a missing Chromebook keeps its tailnet
access indefinitely. Nothing times it out for you. So the first response is
always the same, and it takes about fifteen seconds:

> [Tailscale admin console](https://login.tailscale.com/admin/machines) →
> the kiosk's row → ⋯ → **Remove**. Then rotate `STAFF_PIN` in `.env` on the
> server and `docker compose up -d`.

Do that before worrying about the device itself. What is on it is bounded but
not nothing:

- **Not on it:** `.env`, the database, the admin password, or the staff PIN.
  The PIN is only ever typed into a form and posted to the server — it is never
  rendered into a page, so it cannot be read off the device. It gets rotated
  above because someone may have watched it being typed, not because the
  Chromebook knew it.
- **On it:** the browser profile — `colomeal.queue.v2` (any scans queued during
  an outage: card serials, PUIDs and timestamps) and `colomeal.unrecorded.v1`,
  plus whatever member names and photographs are sitting in the HTTP cache from
  the last service. Modest, but it is student data, and it is the reason the
  device deserves a lock slot and a cupboard rather than a table by the door.

Restricting sudo and disabling VT switching (§2.6.6) are worth doing, but be
clear about what they are for: they stop a curious member with a keyboard, not
someone who walks off with the machine. Physical access defeats both — the boot
menu on this firmware cannot be password-protected. The tailnet ACL above and
the fact that the device holds no credentials are what actually limit the blast
radius; treat the local hardening as the outermost layer, not the load-bearing
one.

---

## Part 4 — Running it

### Verification checklist

Run through this after any change to either side:

```bash
# On the server
docker compose ps                          # api and db up; api bound to 127.0.0.1
curl -fsS http://127.0.0.1:8000/healthz     # {"ok":true,"database":"up"}
tailscale serve status                      # 443 → 127.0.0.1:8000
tailscale status                            # kiosk listed, and how it connects

# On the Chromebook, in Chrome
https://colonial-server.<tailnet>.ts.net/healthz    # {"ok":true,...}
https://colonial-server.<tailnet>.ts.net/           # door screen, padlock, fullscreen
```

`tailscale status` is worth reading closely: a peer line saying `direct` means
the two machines found each other and traffic is peer-to-peer. `relay "xxx"`
means it is going through a DERP relay — still encrypted and still correct, but
slower. `tailscale ping colonial-server` from the kiosk side shows which, and
`tailscale netcheck` shows whether the kiosk's network is blocking the UDP that
direct connections need. Campus and guest networks often do. A relayed
connection is perfectly usable for this app; it is just worth knowing which one
you have before diagnosing a slow door.

### What breaks, and how it shows

| Failure | At the door | Recovery |
| --- | --- | --- |
| Server down, kiosk already open | Scans queue locally, "Saved offline" | Auto-syncs within ~20s of the server returning |
| Server down, kiosk rebooted | Opens from the service-worker cache, banner reads Offline | Same, provided the profile survived |
| Tailscale down on either end | Identical to the server being down — the app cannot tell the difference | Reconnect; the queue drains |
| Tailscale key expired | Same again, but it will not come back on its own | Re-authenticate. Prevent it: disable key expiry (§1.3) |
| Queued scan older than 24h | Cannot be filed under the right day; moves to the red **scans not recorded** badge | Staff enter it by hand from `/admin` |
| Chromebook in Guest mode | No cache, no queue — a dead page when the server is away | Sign into the club account |

The red **scans not recorded** badge in the kiosk header is the only sign that
something was dropped. It lists the card or name, the time and the reason, and
stays until staff clear it. Someone should be told to look at it.

### Updates

```bash
cd ColoMealCheck
git pull                                    # or: docker compose -f docker-compose.deploy.yml pull
docker compose up -d --build
docker compose logs -f api                  # migrations run on start
```

Migrations run from the entrypoint on every boot, so an update is a restart.
Take a database backup first (§5.1) if the release touches the schema.

The Chromebook needs nothing on update — the service worker is network-first,
so the next load fetches the new `kiosk.js` and refreshes the cache behind it.
A kiosk cannot get stuck on last month's rules.

---

## Part 5 — Backups and exports

Two different things that get confused with each other:

- A **backup** is the whole database, in a form nobody reads and everybody can
  restore from. It is insurance.
- An **export** is one report as a CSV, for a treasurer's spreadsheet. It holds
  no card serials, no photographs, no audit trail and no settings, and it
  cannot be restored from. It is a report.

Do both. Neither substitutes for the other.

### 5.1 Back up the database container

Postgres is never published to a host port (§3), so there is no `pg_dump -h`
from outside — the dump has to be taken from inside the `db` container and
piped out:

```bash
cd ColoMealCheck                       # the directory holding .env
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
  | gzip > db-$(date +%F).sql.gz
```

Four details in that line, each of which breaks something if dropped:

- **`-T`** turns off TTY allocation. Without it Docker mangles the stream on
  its way to `gzip`, and the corruption shows up months later at restore time.
- **`sh -c '…'`** with single quotes lets `$POSTGRES_USER` and `$POSTGRES_DB`
  expand *inside* the container, so the command keeps working if those values
  are changed in `.env`. Defaults are `colonial` / `colomealcheck`.
- **`--clean --if-exists`** makes the dump restorable over a database that
  already has tables — which is the situation you will actually be in.
- **Run it from the compose directory**, or Compose cannot find the project.
  Add `-f docker-compose.deploy.yml` if that is the file the stack was
  started with.

The stack can stay up. `pg_dump` takes a consistent snapshot, and a meal being
served during the backup is not a problem.

Then check that the dump is whole, because a truncated one looks fine until it
is needed:

```bash
gunzip -c db-$(date +%F).sql.gz | grep -q "PostgreSQL database dump complete" \
  && echo "dump ok" || echo "TRUNCATED"
```

`pg_dump` writes that marker only after it has finished, so its absence means
the file is a fragment. Grep for it rather than reading the last line — recent
Postgres versions add a `\unrestrict` line and a blank line after the marker,
so `tail -1` shows neither an error nor the marker.

### 5.2 The photos are a second volume

The database holds who and when; `photos` holds the faces. A database dump
restored on its own gives you a working system where every enrolled member has
no photograph.

```bash
docker run --rm -v colomealcheck_photos:/p -v "$PWD":/out alpine \
  tar czf /out/photos-$(date +%F).tar.gz -C /p .
```

The volume name is the project name (the directory) plus `_photos`. Confirm it
with `docker volume ls` rather than assuming — a clone into a differently named
directory gets a differently named volume.

### 5.3 Put it on a schedule

```cron
15 4 * * * cd /home/colonial/ColoMealCheck && docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' | gzip > /var/backups/colomeal/db-$(date +\%F).sql.gz
30 4 * * 0 docker run --rm -v colomealcheck_photos:/p -v /var/backups/colomeal:/out alpine tar czf /out/photos-$(date +\%F).tar.gz -C /p .
45 4 * * * find /var/backups/colomeal -name '*.gz' -mtime +30 -delete
```

`%` must be escaped as `\%` in a crontab — unescaped it is read as a newline,
and the command silently becomes something else. Photos change slowly enough
that weekly is honest; the database is worth doing nightly.

Copy the files off the server — a backup on the machine it protects is not a
backup. They contain PUIDs and photographs of students, so where they land is a
decision the club makes deliberately, not a detail.

### 5.4 Restore

```bash
docker compose stop api          # nothing should be writing during a restore
gunzip -c db-2026-08-24.sql.gz \
  | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker run --rm -v colomealcheck_photos:/p -v "$PWD":/in alpine \
  tar xzf /in/photos-2026-08-24.tar.gz -C /p
docker compose start api
docker compose logs -f api
```

`--clean --if-exists` in the dump drops the old objects on the way in, so this
replaces the database rather than merging into it. Migrations run from the
entrypoint when `api` starts, so a dump taken against an older schema is
brought up to date automatically — restore first, start second, in that order.

Then confirm: `/healthz` returns `{"ok":true,"database":"up"}`, `/admin` shows
the expected members, and a member page shows a photograph.

**Test a restore once, before it matters**, into a scratch stack rather than
over the live one:

```bash
COMPOSE_PROJECT_NAME=colomealrestore API_PORT=127.0.0.1:8001 docker compose up -d
```

That builds a separate set of volumes under a separate project name and leaves
the real stack alone. Tear it down with
`COMPOSE_PROJECT_NAME=colomealrestore docker compose down -v` when the check
passes. An untested backup is a hope, not a plan.

### 5.5 Export CSVs from the admin console

Every export is behind a staff sign-in at `/login` — any staff account, not
only an admin. Each one downloads with a dated filename.

| Page | Button | File | What is in it |
| --- | --- | --- | --- |
| `/admin` (Today) | **Export CSV** | `attendance-<date>.csv` | every scan that day — period, kind, member or guest or alumni identity, entry method, overage flag, timestamp |
| `/admin/reports` | **CSV** on Weekly usage | `weekly-usage-<start>-to-<end>.csv` | per member: meals used, allotment, over-by, guest meals hosted |
| `/admin/reports` | **CSV** on Overages | `overages-<YYYY-MM>.csv` | members over plan that month, and by how many |
| `/admin/reports` | **CSV** on Guest meals | `guest-meals-<YYYY-MM>.csv` | hosts and the guest meals each signed in |
| `/admin/reports` | **CSV** on Enrollment gaps | `enrollment-gaps.csv` | active members with no card enrolled |
| `/admin/analytics` | **CSV** | `member-analytics-<start>-to-<end>.csv` | the table exactly as shown — same filters, same sort |

The period comes from the page, not from the button. On `/admin/reports` set
**Week of** and **Month in** and press Update first; the CSV links then carry
the period being displayed. On Today, pick the date and press Go. On Analytics,
the search box, class year, plan, status and current sort are all folded into
the CSV link, so filter the screen down to what you want and then click CSV —
what downloads is what you are looking at.

The same exports by URL, for a fixed period or a script:

```
/admin/reports/daily.csv?week=2026-08-24        # note: the day goes in `week`
/admin/reports/weekly-usage.csv?week=2026-08-24 # any date inside the week
/admin/reports/overages.csv?month=2026-08-01    # any date inside the month
/admin/reports/guests.csv?month=2026-08-01
/admin/reports/enrollment-gaps.csv
/admin/analytics.csv?start=2026-08-01&end=2026-08-24
```

The daily export really does take its day in a parameter named `week` — it
shares a route with the weekly reports. It looks wrong and it is correct. An
unparseable or missing date falls back to today rather than erroring, so a
typo returns an unexpectedly ordinary-looking file; check the filename, which
always names the period actually exported.

To pull one on a schedule, sign in first and keep the cookie:

```bash
BASE=https://colonial-server.<tailnet>.ts.net
curl -sS -c /tmp/cj -o /dev/null -X POST \
  -d "username=admin" -d "password=$ADMIN_PASSWORD" "$BASE/login"
curl -fsS -b /tmp/cj -o weekly-usage.csv \
  "$BASE/admin/reports/weekly-usage.csv?week=$(date +%F)"
head -1 weekly-usage.csv     # the header row: last_name,first_name,puid,...
```

Run it from a machine on the tailnet. A wrong password does not fail loudly:
the login returns 401 with the sign-in page and sets no cookie, and the export
then answers `{"detail":"Sign in required"}` — which, without `-f`, curl writes
into `weekly-usage.csv` as if it were the report. `-f` on the second call makes
that a non-zero exit instead of a bad file, which is what a cron job needs.
Keep the password out of shell history: read it from a file or the environment.

None of these files is a backup. They carry no card serials, no photographs, no
audit trail, no schedule and no settings, and there is no import that reads them
back — roster import (`/admin/members/import`) reads a roster, not a report.
They do carry names and PUIDs, so they deserve the same care as §5.3's files.

---

## Open items

Things this deployment leaves deliberately undone, so they are decisions rather
than oversights:

- **The image tag is `:latest`.** Pin it to a commit SHA before the club relies
  on the system (§1.2).
- **The session cookie is not `Secure`-only.** `app/main.py` sets
  `https_only=False` because the LAN deployment is plain HTTP. Once everything
  goes through `tailscale serve`, that can be tightened to `True` — but only
  after confirming nobody reaches the admin screens over plain HTTP.
- **The staff PIN is shared and the check-in path has no login.** Both are right
  for a dining room and both are audited — every override and forced entry has
  an actor in `/admin/audit`. They stop being right if this is ever reachable
  beyond the tailnet.
- **ChromeOS VPN routing is verified, not assumed.** §2.2 tests it rather than
  trusting it. If the test fails, the mini-PC fallback is the answer.
- **No monitoring.** Nothing tells anyone the server is down except a member
  standing at a door that says Offline. A cron job curling `/healthz` from a
  tailnet machine and mailing on failure would close that.
