# ColoMealCheck

Meal attendance for the Colonial Club of Princeton. Members tap their TigerCard
on a reader at the dining room door — or type their PUID if they left it in
their room — and the system records the meal, tracks it against their weekly
plan (19, 14 or the 9-meal RCA/PAA plan), and manages the two guest meals each
member gets per month.

The server runs in Docker on the club LAN. The kiosk is a laptop with an HID
reader, running Chrome pointed at it.

---

## Start here: verify the card serial is stable

**Do this before anything else.** The system identifies members by their card's
CSN (serial number), which any reader can read without Princeton's encryption
keys. That works on iCLASS 202x/SE cards, whose CSN is fixed.

It does **not** work on iCLASS **Seos** cards, which emit a 4-byte *random*
identifier by design, specifically to prevent this kind of tracking. If
Princeton has migrated to Seos, every tap looks like a different card.

Borrow 8–10 TigerCards across class years and tap each one three times. If a
card reports a different value each time, stop and pick a fallback:

- **125 kHz Prox side.** If TigerCards are dual-technology, the Prox number is
  stable and needs no key. Use a prox reader with a Wiegand-to-USB converter and
  enroll with `credential_type: "prox"` — nothing else changes.
- **Manual PUID only.** Already built and fully supported. Members type their ID;
  every rule behaves identically. Slower at the door, but correct.
- **Talk to the ID office.** A reader keyed to Princeton's format outputs the
  real PACS number tied to the printed PUID. Enroll with `"pacs"`.

The credential layer is deliberately abstract (`app/services/credentials.py`),
so any of these swaps in without touching the rules engine or the schema.

---

## Running it

```bash
cp .env.example .env
# Set SECRET_KEY (the file tells you how to generate one), POSTGRES_PASSWORD,
# and STAFF_PIN. Then:
docker compose up -d
docker compose logs api        # the first-boot admin password prints here once
```

The app is at `http://<server-host>:8000`:

| Path | What it is |
| --- | --- |
| `/` | The kiosk. Tap a card, or click into the **Enter your Princeton ID** box and type a PUID — nine digits, nothing else gets in. |
| | A banner across the top reads **Now serving Dinner · 57:46 left** while a meal is running, and **Next Meal: Breakfast · in 12:30:15** when it is not. Both countdowns tick live. |
| `/enroll` | Staff page for enrolling a member and their card. Gated by the staff PIN. |
| `/admin` | The office: roster, analytics, schedule, reports, audit. Gated by a staff login. |

Enrolling is what *creates* a member. The usual case is someone who is not in
the system at all, so the form asks for first name, last name, PUID, NetID, class
year and meal plan alongside the card and the photo, and writes the person and
their credential in one transaction — there is never a member row with no way to
check in, or a card bound to nobody. A second mode on the same page, **Already in
the system**, handles the replacement TigerCard: find the member, tap the new
card, and their old one is retired automatically.

A member is identified by **two** things, and both are required: the PUID
printed on their card, and the NetID every other Princeton system knows them by.
The NetID is what makes a roster reconcilable against anything outside this app,
and enrollment is the only moment the member is standing there to be asked for
it — so unlike a guest's, it has no "has none" escape hatch. Every member of this
club is a Princeton affiliate who has one. It is stored lowercase, unique across
members, searchable anywhere a PUID is, and carried into every member CSV.

The column itself is nullable, because members enrolled before it existed have
no NetID on file and inventing one would be worse than leaving the gap visible.
The members list prints a dash for each of them — that dash is the backfill
worklist, and the admin edit form is where it gets filled in, which is why blank
is accepted there and refused at the kiosk.

The three identifiers are checked for shape before anything is written: a card
CSN must be 16 hexadecimal digits, a PUID nine decimal digits, and a NetID 2–8
letters and digits starting with a letter. Case and the separators some readers
emit are normalized away first, so `04a1-b2c3-d4e5-f601` is accepted and stored
as `04A1B2C3D4E5F601`, and `SOkafor` as `sokafor`. Every rule lives on the page
*and* on `/api/enroll` and `/api/enroll/new` — the page for a message staff can
act on while the member is still standing there, the API so a truncated read
cannot become a card that never scans. A PUID or NetID already on file is
refused by name, never merged: at this desk a clash is almost always a typo, and
quietly attaching a second row to a person already enrolled is far harder to
notice than an error message. Scans themselves are deliberately unchecked, so
credentials enrolled before these rules keep working.

The page says so at the field, as it is typed, rather than saving the complaint
for the submit button: a bad card read is far cheaper to fix while the card is
still in someone's hand. The message appears 200ms after typing stops — the
reader types all 16 digits in a burst, and per-keystroke checking would flash a
complaint across every good tap — and disappears the instant the value is right.

Both conflict checks refuse rather than merge. A PUID already on file names the
person holding it; a card that already scans as somebody else is treated as a
mis-tap. At an enrollment desk both are almost always a typo or a stray tap, and
quietly moving a card off an existing member is far harder to notice than an
error message.

Enrollment is a separate page rather than a panel over the kiosk: it is slow,
staff-driven work involving a photo, and it must not occupy the check-in screen
while a queue is forming behind it. When an unrecognized card is tapped, the
kiosk shows a red band with a **Staff: link this card** button that carries the
card value across to `/enroll`, so nobody has to tap twice.

Two containers, no reverse proxy: Uvicorn serves the API, the pages and the
photos directly. Postgres and the photo directory are on named volumes.

### Running from the published image

For someone who wants to try the kiosk without a copy of the source — a friend
testing it, or a spare laptop that has Docker and nothing else.
`docker-compose.deploy.yml` pulls a built image from GHCR instead of building
`api` from this directory, and reads the same `.env` as the file above.

The image is private, so pulling it needs a GitHub token with `read:packages`:

```bash
docker login ghcr.io -u <your-github-username>
docker compose -f docker-compose.deploy.yml up -d
docker compose -f docker-compose.deploy.yml logs api   # admin password, once
```

Access is governed by this repository's collaborator list rather than by a
permission list of its own, which is what the `org.opencontainers.image.source`
label in the Dockerfile buys: it links the package to the repository, so adding
someone here is all it takes.

That file tracks `:latest`, which CI rewrites on every merge to `main`. Anyone
running it gets each change on their next `docker compose pull` — convenient
while the app is being tried out, and a hazard once a meal service depends on
it, because a restart can move the kiosk onto a build nobody chose.

Pin it before that point. Every CI build also publishes an immutable
`sha-<commit>` tag, so there is always a specific thing to pin to.

CI publishes on its own; the rest of this section is for doing it by hand, which
is worth knowing when the workflow is the thing that is broken.

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/anagkan/colomealcheck:0.2.0 \
  -t ghcr.io/anagkan/colomealcheck:latest \
  --push .
```

Both architectures, always. This is built on an Apple Silicon Mac and will be
run on whatever the club has lying around; an image pushed for one architecture
fails on the other with `exec format error`, which says nothing about what
actually went wrong. That needs a builder using the `docker-container` driver —
the default one silently cannot produce a multi-architecture manifest, and the
`--platform` flag above appears to be accepted either way:

```bash
docker buildx create --name colomeal --driver docker-container --bootstrap --use
```

Check the result before handing it to anyone — and if `docker-compose.deploy.yml`
has been pinned to a version by then, bump it to match:

```bash
docker buildx imagetools inspect ghcr.io/anagkan/colomealcheck:0.2.0
```

Both `linux/amd64` and `linux/arm64` should be listed. The `unknown/unknown`
entries alongside them are build provenance, not stray platforms.

### Kiosk laptop

```bash
./run-kiosk.sh colonial-server.local 8000
```

This launches Chrome in kiosk mode with two flags that are **not optional**:

```
--unsafely-treat-insecure-origin-as-secure=http://<host>:8000
--user-data-dir=/tmp/colomealcheck-kiosk
```

The webcam API (`getUserMedia`) only exists in a *secure context*. A plain-HTTP
LAN address is not one, so without these flags `navigator.mediaDevices` is
`undefined` and enrollment photos fail **silently**. The flag is also ignored
unless a separate profile directory is given — both flags, or neither works.
The enrollment screen detects the missing API and says so, with a file-upload
fallback, rather than offering a dead button.

If you later want the app reachable from more than the one laptop, put TLS in
front of it. Nothing in the app assumes HTTP.

### Reader setup

Configure the OMNIKEY 5427CK in **keyboard wedge** mode with a suffix of Enter.
Before opening the app, tap a card into a text editor and confirm digits appear
followed by a newline. Reader configuration problems surface here, not in the
app. A distinctive prefix is worth setting so stray typing can never be mistaken
for a scan.

---

## How the rules work

Every rule lives in `app/services/scan.py::process_scan`, and the entry method
(tapped card vs typed PUID) changes nothing except one recorded field.

| Situation | What happens |
| --- | --- |
| Meal within plan | Checked in. Green band with the running count. |
| **Past the weekly allotment** | **Recorded anyway**, flagged as an overage, amber band. Never blocked. |
| Second scan, same meal period | Refused as a duplicate, and the guest popup opens with the member filled in — a second tap almost always means they are signing a guest in. Staff can force a genuine second meal; it's audited. |
| Membership not `active` | Red banner, meal **still recorded** and flagged. |
| Outside serving hours | Nothing recorded; the member is still shown. |
| **Third guest meal in a month** | **Blocked.** Staff override with a typed reason releases it, into the audit log. |
| Alumni meal | Recorded against nobody, no quota. Needs a name, a class year and one contact detail. |
| Unrecognized card | Staff-gated enrollment: create the member, capture a photo, bind the card. |

Design decisions worth knowing:

- **Overages warn, they never block.** A counter is not a reason to turn away
  someone holding a tray. The overage list is a billing report, resolved later.
- **Guest meals are a separate bucket.** Hosting three guests does not spend any
  of a member's own 19, 14 or 9, and a member over their weekly plan still has
  guest meals available.
- **A guest is identified, not just labelled.** The popup records the guest's
  first name, last name and Princeton NetID against the hosting member, so the
  club can still answer "who was this?" a month later. It opens from the **Guest
  Meal** button at the bottom of the kiosk — which asks who is hosting — or from
  a member's second tap in a meal period, which fills the host in already.
- **A guest with no NetID says so, and why.** Visiting parents, alumni and
  siblings have none, so the popup offers a **Guest has no NetID** tick box that
  swaps the field for a required reason. The field can never simply be left
  blank: a blank is indistinguishable from a rush at the door, and the reason is
  what makes the row traceable afterwards. Exactly one of the two is ever stored,
  and both appear in the daily attendance CSV.
- **An alumni meal belongs to nobody.** An alum is not on the roster, hosts
  nobody and is hosted by nobody, so the **Alumni Meal** button at the bottom of
  the kiosk asks for no member at all and the row it writes is the only record
  the meal leaves: first name, last name, class year, and an email address *or* a
  phone number — either one is enough, but not neither. That last rule is the
  whole point of the popup. A NetID is asked for too and is the one **optional**
  field: many alumni keep one for life and it is the cleanest link back to the
  member they used to be, but plenty have let theirs lapse — and a NetID
  identifies an alum without reaching them, so it can neither be required nor
  stand in for a contact detail. Given one, it is format-checked and stored
  lowercase like every other NetID here. There is no card, PUID or NetID to recover a contact
  detail from once the alum has walked away, so a record nobody can be reached
  from is worth no more than no record at all. Alumni meals draw down no
  allotment and no guest quota, are counted separately on the day view, and carry
  their identity into the daily attendance CSV. This is the only kind of meal
  whose `member_id` is NULL, which is why every per-member report filters on
  `kind` — see `app/models.py::Attendance`.
- **The meal week starts Monday.** A Sunday dinner is the last meal of its week;
  Monday breakfast opens a fresh allotment. Nothing rolls over.
- **Guest quota resets on the 1st** of the calendar month, not on a rolling
  30-day window.
- **Service dates, not timestamps.** Every meal stores a `service_date` computed
  in club-local time at write time, and every report groups by it. This is what
  makes the DST transitions non-events — see the tests in `tests/test_periods.py`
  that pin down both of them.

The 19-meal plan means "every meal we serve": the default schedule is
breakfast/lunch/dinner on weekdays plus brunch/dinner on weekends, which is
exactly 19 windows. The admin schedule page shows this count and warns if an
edit breaks it. The 14-meal plan is just a smaller number — a 14-plan member
eating breakfast spends one of their 14 on it. The **RCA/PAA** plan is smaller
again at 9 meals a week, for RCAs and PAAs who eat here part-time; it carries
the same two guest meals a month as every other plan, since the guest quota is
club-wide and does not scale with the plan.

| Meal | Days | Window |
| --- | --- | --- |
| Breakfast | Mon–Fri | 8:00 – 10:00 am |
| Lunch | Mon–Fri | 11:45 am – 1:45 pm |
| Brunch | Sat–Sun | 11:30 am – 1:30 pm |
| Dinner | every day | 5:45 – 7:45 pm |

These are the seeded defaults in `app/seeds/meal_periods.py`. `seed_meal_periods`
only populates an **empty** table, so changing that file does not touch a
database that already has a schedule — edit those windows at `/admin/schedule`,
or update the rows directly. Editing times in place keeps the row IDs, so past
attendance stays attached to the meal it was eaten at.

All of the numbers (19, 14, 9, 2, the week start day) live in the `settings` table
and are editable at `/admin/settings`. There are no magic numbers in the code.

---

## Day one, before anyone has a card

Manual PUID entry needs no enrollment. Create the roster in `/admin/members`,
and members can eat immediately by typing their ID at the kiosk while cards get
linked over the first week or two. `/admin/reports` has an **enrollment gaps**
list — active members with no card linked — which is the list to work through.

---

## Analytics

`/admin/reports` answers *who owes what this week*. `/admin/analytics` answers
*how is the club actually being used* — over any date range (default: the last
30 days), sliceable by class year, meal plan, status, name or PUID.

Every column in the member table sorts, by clicking its heading: meals eaten,
meals per week, share of plan used, days attended, guest meals hosted, overages,
last seen, and the member fields themselves. Above it are club-wide breakdowns
by class year, plan and status, each showing headcount, meals, meals per member
and what share of the group ate at all. The breakdowns deliberately describe the
whole club rather than the filtered slice — otherwise filtering to one class
year would show that class as 100% of the membership.

The **CSV** button exports exactly what is on screen: same window, same filters,
same order.

Two derived columns are worth knowing:

- **Plan used** is meals-per-week against the member's weekly allotment. It is
  blank rather than 0% for members with no plan — there is no denominator, and
  0% would read as "never eats" instead of "pays per meal".
- **Dormant** counts members who are `active` but ate nothing in the window.
  That is either someone who has quietly stopped eating here or a card that was
  never enrolled properly; both are worth a look.

Sorting puts rows with nothing to sort by at the bottom in *both* directions.
"No class year on file" is not the highest class year, and a member who has
never eaten here is not the most recently seen.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
npm ci                            # jsdom, for the browser JavaScript tests
.venv/bin/python -m pytest        # 336 tests, ~15s
```

Tests run against SQLite, which honours the same partial unique indexes used on
Postgres, so the duplicate guard is exercised for real rather than mocked. The
API tests render every admin template, so a broken Jinja reference fails in CI
rather than in front of a club officer.

`tests/test_kiosk_dom.py` runs the real `kiosk.js` against the real rendered
kiosk page under jsdom. It exists because the kiosk has two input paths
competing for one keyboard — the HID reader (which *is* a keyboard, typing into
a hidden sink) and a person typing their PUID. Getting focus wrong there
silently submits someone's ID as a card number, or appends a previous person's
half-typed digits to the next card scan. Both have happened.

The rule the tests pin down: **the reader sink owns the keyboard only on the
idle and result screens.** On any screen with something to type into, the human
owns it — and on every screen transition back, focus is *forcibly* reclaimed
and both buffers cleared, rather than politely requested.

Skipped if node or jsdom is missing, so contributors without node still get a
clean run. Set `REQUIRE_DOM_TESTS=1` to turn that skip into a hard error — CI
sets it, because there a missing install is indistinguishable from a pass.

It also covers the meal banner's two countdowns. Both count down from a
*duration* the server supplies, re-synced every minute, rather than from a
timestamp compared against the laptop's own clock — a kiosk with a wrong clock
would otherwise display a nonsense countdown. `seconds_remaining` and
`next_period` in `services/periods.py` convert to UTC before subtracting, so a
window crossing the spring-forward reports the two hours actually left rather
than the three the clock face moved. When either countdown reaches zero the
kiosk re-asks the server what is true rather than assuming the next state.

`next_period` searches a full week ahead, so after Sunday dinner the banner
names Monday breakfast rather than the following weekend's brunch, and it orders
candidates by clock time rather than `sort_order` — that column is a display
preference and nothing stops it disagreeing with the timetable. With no active
windows at all there is no next meal to name, and the banner falls back to
**Outside Meal Hours**.

The same file runs `enroll.js` against the rendered `/enroll` page, for a
related reason: that page submits to two different endpoints depending on which
mode it is in. Posting a new member to the link-a-card endpoint merely fails,
but posting a replacement card to the create endpoint would try to duplicate a
person who already exists. Which endpoint gets picked is only observable from a
browser, so it is asserted there.

```
app/
  services/           the rules. scan.py is the one decision function.
    scan.py           check in, guest meal, alumni meal, undo
    periods.py        instant -> (service_date, meal period); week/month bounds
    allotment.py      weekly usage vs plan
    guests.py         monthly guest quota
    alumni.py         contact details for a meal with no member behind it
    netid.py          the NetID rule, shared by members and guests
    credentials.py    card <-> member binding, reissue, revocation
    reports.py        aggregations, member analytics and CSV
  routers/            HTTP. Thin — all logic is in services/.
  models.py           schema, including the two partial unique indexes
  templates/kiosk/    the door screen
  templates/admin/    the office screens
tests/                336 tests; start with test_scan_rules.py
```

Schema changes: edit `app/models.py`, then
`alembic revision --autogenerate -m "..."`. The initial revision builds from
metadata deliberately (the partial indexes are dialect-specific); later ones
should be ordinary autogenerated revisions.

### Backups

Two volumes matter: `pgdata` and `photos`.

```bash
docker compose exec -T db pg_dump -U colonial colomealcheck | gzip > backup-$(date +%F).sql.gz
docker run --rm -v colomealcheck_photos:/p -v "$PWD":/out alpine \
  tar czf /out/photos-$(date +%F).tar.gz -C /p .
```

Worth putting on a cron job to somewhere off the server.

---

## Before launch: things for the club to decide

The system stores PUIDs and photographs of students. Worth agreeing with club
officers, and writing down:

- Who gets an admin account, and who only gets staff access.
- How long attendance history is retained. A year covers any billing dispute.
- Where backups live and who can read them.

Two security notes on the current deployment: the app trusts the club LAN
(check-in needs no login, since a queue at dinner is no place for one), and the
staff PIN is shared. Both are appropriate for a dining room and both are logged
— every override and forced entry has an actor in `/admin/audit`. If the server
is ever exposed beyond the club LAN, revisit both.

---

## License

Licensed under the Apache License, Version 2.0. The full terms are in
[`LICENSE`](LICENSE); the attribution notice is in [`NOTICE`](NOTICE).

You may use, modify and redistribute this, including for another eating club.
In return the license asks three things: pass along a copy of the license, mark
any files you changed as changed, and carry the `NOTICE` file forward so the
original authorship stays attached to the code.
