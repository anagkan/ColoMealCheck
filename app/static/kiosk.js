/* Kiosk controller.
 *
 * Three things matter here and everything else is presentation:
 *   1. The hidden reader input must never lose focus on the screens where the
 *      next event is a card tap, or the reader types into the void and
 *      check-ins silently stop working.
 *   2. Typed-ID entry and card entry post to the same endpoint with the same
 *      shape, so the two paths cannot diverge.
 *   3. If the server is unreachable, scans are queued locally and replayed —
 *      a network blip during dinner must not cost anyone a meal.
 *
 * Card enrollment deliberately lives on its own page (/enroll): it is slow,
 * staff-driven work that must not occupy the check-in screen while a queue is
 * forming behind it.
 */
(function () {
  "use strict";

  const CONFIG = window.KIOSK_CONFIG || { resultSeconds: 6, undoSeconds: 60 };
  const QUEUE_KEY = "colomeal.queue.v2";
  const LEGACY_QUEUE_KEY = "colomeal.queue.v1";

  const el = (id) => document.getElementById(id);
  // Two screens, not three: typed-ID entry is a panel on the idle screen rather
  // than a screen of its own, so a member whose card fails never has to go
  // looking for it.
  const screens = {
    idle: el("idle"),
    result: el("result"),
  };

  const cardInput = el("cardInput");
  const manualInput = el("manualInput");
  let current = "idle";
  let resultTimer = null;
  let lastResult = null;
  let lastScan = null; // { value, credentialType } — what "check in anyway" re-sends

  /* ---------------- reader focus watchdog ----------------
   *
   * The reader IS a keyboard, so the hidden sink and any human typing compete
   * for the same keystrokes. The rule that keeps them apart: the sink owns the
   * keyboard only on the screens where the next event is a card tap. On every
   * screen with something to type into — the ID box, the guest popup, the staff
   * PIN — the human owns it and the sink must stay out of the way.
   *
   * The popups are the one case where that is not decided by the screen
   * underneath: they open over the idle or result screen, which the reader
   * would otherwise own, so an open popup takes the keyboard back for the
   * human. Both popups are all typing, so the rule is the same for either.
   *
   * Losing focus on the idle screen is still the #1 way a kiosk dies quietly,
   * so on those two screens the watchdog is as aggressive as it can be. */

  const READER_SCREENS = new Set(["idle", "result"]);

  function aModalIsOpen() {
    return guestModalIsOpen() || alumniModalIsOpen() || unrecordedModalIsOpen();
  }

  function readerOwnsKeyboard() {
    return READER_SCREENS.has(current) && !aModalIsOpen();
  }

  function focusReader() {
    if (!readerOwnsKeyboard()) return;
    const active = document.activeElement;
    if (active && active !== cardInput && active.tagName === "INPUT") return;
    if (active && active.tagName === "TEXTAREA") return;
    cardInput.focus();
  }

  function releaseReader() {
    // Drop any characters the sink swallowed before we took focus away, so a
    // half-typed ID cannot prepend itself to the next person's card.
    cardInput.value = "";
    if (document.activeElement === cardInput) cardInput.blur();
  }

  function claimReader() {
    // Used on screen transitions, where the polite "don't steal focus from
    // another input" rule in focusReader() would otherwise leave focus sitting
    // in the ID box after a member cancels — and the next person's card tap
    // would append itself to whatever they had typed.
    const active = document.activeElement;
    if (active && active !== cardInput && typeof active.blur === "function") {
      active.blur();
    }
    cardInput.value = "";
    cardInput.focus();
  }

  /* ---------------- screens ---------------- */

  function show(name) {
    // Never leave a member's half-typed ID sitting in the box for the next
    // person to find, or to be appended to. The box sits on the idle screen
    // itself, so every transition clears it — there is no "manual screen" to
    // have been left for this to be conditional on.
    manualInput.value = "";
    el("manualError").hidden = true;
    // The screen under a popup is changing, so the popup no longer belongs to
    // anything. Closing it here does not loop: no close function calls show.
    if (guestModalIsOpen()) closeGuestModal();
    if (alumniModalIsOpen()) closeAlumniModal();
    if (unrecordedModalIsOpen()) closeUnrecordedModal();

    Object.entries(screens).forEach(([key, node]) => {
      node.hidden = key !== name;
    });
    current = name;
    if (resultTimer) {
      clearTimeout(resultTimer);
      resultTimer = null;
    }
    if (name === "idle") lastResult = null;
    if (readerOwnsKeyboard()) {
      claimReader();
    } else {
      releaseReader();
    }
    window.scrollTo(0, 0);
  }

  function backToIdle() {
    show("idle");
  }

  cardInput.addEventListener("blur", () => setTimeout(focusReader, 40));
  document.addEventListener("click", () => setTimeout(focusReader, 40));
  setInterval(focusReader, 1500);

  cardInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const value = cardInput.value.trim();
    cardInput.value = "";
    // Belt and braces: if a stray focus ever lands here while someone is
    // typing, do not submit their ID as a card number.
    if (!value || !readerOwnsKeyboard()) return;
    // A card tap while a result is on screen is the next person in line.
    submitScan(value, "csn");
  });

  /* ---------------- network ---------------- */

  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      // Only a string is showable on the band; anything else becomes
      // "[object Object]" in front of a queue.
      const detail = typeof body.detail === "string" ? body.detail : "";
      const error = new Error(detail || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  /* ---------------- local storage ----------------
   *
   * localStorage is not guaranteed to work. A private-browsing window and a full
   * disk both make setItem throw, and an unguarded throw here escaped
   * submitScan's own catch block — leaving the member looking at the previous
   * screen with no answer at all, which is the one thing the offline path exists
   * to prevent. Every access goes through these two, and a page that cannot
   * reach the disk falls back to memory for its own lifetime: worse than disk,
   * but a session that keeps working beats one that silently stops. */

  const memory = {};
  // Keys whose last write did not reach the disk. For those the in-memory copy
  // is the only correct one, so reads must not fall back to a stale disk copy.
  const unpersisted = new Set();

  function readList(key) {
    if (unpersisted.has(key)) return memory[key] || [];
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      // Unreadable or corrupt: either way the disk copy cannot be trusted again.
      unpersisted.add(key);
      return memory[key] || [];
    }
  }

  function writeList(key, items) {
    memory[key] = items;
    try {
      localStorage.setItem(key, JSON.stringify(items));
      unpersisted.delete(key);
    } catch (err) {
      unpersisted.add(key);
    }
  }

  /* ---------------- offline queue ----------------
   *
   * Every kind of meal queues, not just a member's own check-in: a guest meal
   * and an alumni meal are just as lost if the network drops while somebody is
   * standing there, and they are the two that cannot be reconstructed afterwards
   * from a card. So a queue entry names where it was going —
   *
   *     { path, body, label }
   *
   * — and the replay is the same request the popup would have made, which is
   * what keeps the offline path from becoming a second, subtly different way to
   * record a meal. `label` is only for the screen: it is what the entry is
   * called if it ends up in front of staff. */

  // Matches MAX_REPLAY_AGE_SECONDS in routers/api_scan.py. Past this the server
  // refuses to believe the claimed time and files the meal under whenever it
  // finally arrived — the wrong service date and the wrong meal week. The kiosk
  // holds the same number so it can stop sending them rather than let them be
  // misfiled; the server's copy is still the rule, this one just acts sooner.
  const REPLAY_WINDOW_SECONDS = 24 * 60 * 60;

  // A whole dinner service is a few hundred scans at the very most, so passing
  // this means something pathological rather than a busy night.
  const MAX_QUEUE = 500;

  function readQueue() {
    return readList(QUEUE_KEY);
  }

  function writeQueue(items) {
    let kept = items;
    if (kept.length > MAX_QUEUE) {
      // The newest are the ones kept: the oldest are closest to ageing out of
      // the replay window, and a scan the server will not accept a time for is
      // the least useful thing in here. They are not dropped, though — an
      // unrecorded meal always goes in front of a human.
      const lost = kept.slice(0, kept.length - MAX_QUEUE);
      kept = kept.slice(kept.length - MAX_QUEUE);
      lost.forEach((item) =>
        addUnrecorded(item, "Queue overflowed — this scan was never sent.")
      );
    }
    writeList(QUEUE_KEY, kept);
    const badge = el("offlineBadge");
    if (kept.length) {
      badge.hidden = false;
      badge.textContent = `Offline — ${kept.length} queued`;
    } else {
      badge.hidden = true;
    }
  }

  function enqueue(path, body, label) {
    const items = readQueue();
    // An identical request is one submit enqueued twice, not two meals:
    // occurred_at is stamped per submit, so a genuine second meal a moment later
    // carries a different one and still queues.
    const serialized = JSON.stringify(body);
    const seen = items.some(
      (item) => item.path === path && JSON.stringify(item.body) === serialized
    );
    if (seen) return;
    items.push({ path: path, body: body, label: label });
    writeQueue(items);
  }

  function ageSeconds(item) {
    const at = Date.parse((item.body && item.body.occurred_at) || "");
    // No claimed time means the server will use its own, which is never stale.
    if (isNaN(at)) return 0;
    return (Date.now() - at) / 1000;
  }

  /* v1 of the queue held bare /api/scan bodies, from before guest and alumni
   * meals could queue at all. A kiosk updated in the middle of an outage would
   * otherwise throw away whatever was still waiting under the old key — the one
   * thing this whole mechanism exists to prevent — so they are carried across
   * once and the old key is emptied behind them. */
  function migrateLegacyQueue() {
    const legacy = readList(LEGACY_QUEUE_KEY);
    if (!legacy.length) return;
    const items = readQueue();
    legacy.forEach((body) => {
      items.push({ path: "/api/scan", body: body, label: describeScan(body) });
    });
    writeList(LEGACY_QUEUE_KEY, []);
    writeQueue(items);
  }

  // Two flushes overlapping would each read the same queue, send everything
  // twice and then write back a `remaining` that resurrects what the other one
  // had just cleared. Three things start a flush — the timer, the online event
  // and every successful scan — so they really do overlap.
  let flushing = false;

  async function flushQueue() {
    if (flushing) return;
    const items = readQueue();
    if (!items.length) return;
    flushing = true;
    const remaining = [];
    try {
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (ageSeconds(item) > REPLAY_WINDOW_SECONDS) {
          // Sending this would record the meal against today instead of the day
          // it was eaten. A wrong row in the wrong meal week is invisible; an
          // unsent one stays on the badge until somebody deals with it.
          addUnrecorded(item, "Too old to record automatically — add it in Admin.");
          continue;
        }
        let result;
        try {
          result = await post(item.path, item.body);
        } catch (err) {
          if (err.status) {
            // The server answered and refused outright. Retrying will not change
            // its mind, so stop carrying it and put it in front of staff.
            addUnrecorded(item, err.message);
            continue;
          }
          // Still unreachable. Keep this and everything after it for next time
          // rather than working through a queue-length pile of doomed requests.
          remaining.push(...items.slice(i));
          break;
        }
        // A 200 is not proof the meal was recorded. unknown_credential,
        // outside_service and guest_quota_exceeded all answer 200 with no row
        // written, and dropping those on the floor is how a queued meal used to
        // vanish with nothing left behind. attendance_id is the only honest
        // signal — and it is present on already_checked_in too, where the row
        // exists precisely because this scan already got through.
        if (result && result.attendance_id) continue;
        addUnrecorded(
          item,
          (result && result.message) || "The server did not record this scan."
        );
      }
      writeQueue(remaining);
    } finally {
      flushing = false;
    }
  }

  setInterval(flushQueue, 20000);
  window.addEventListener("online", flushQueue);

  /* What a queued meal is called if it ends up in front of staff. A card number
   * is what identifies a check-in — there is no name to use, since being offline
   * is exactly why the kiosk never learned one — while a guest and an alum were
   * both typed in by somebody and can be called by name. */

  function describeScan(body) {
    return `${body.credential_type === "manual_puid" ? "ID" : "Card"} ${body.value}`;
  }

  function describeGuest(body) {
    const who = `${body.guest_first_name} ${body.guest_last_name}`.trim() || "Guest";
    return `Guest ${who}`;
  }

  function describeAlumni(body) {
    const who = `${body.first_name} ${body.last_name}`.trim() || "Alum";
    return `Alumni ${who}`;
  }

  /* ---------------- scans that could not be recorded ----------------
   *
   * The queue keeps a network blip from costing anyone a meal, and it does that
   * by retrying. But some replays can never succeed: an unenrolled card, a quota
   * the server refuses, a scan that sat in the queue past the day it belonged
   * to. Those are the ones worth being loud about — they are meals somebody ate
   * that the system has no row for, and the person who could fix it has usually
   * gone home by the time anyone looks.
   *
   * So they are kept, with the reason, and the badge stays up until staff clear
   * it. The kiosk never again decides on its own that a meal did not happen. */

  const UNRECORDED_KEY = "colomeal.unrecorded.v1";

  function addUnrecorded(item, reason) {
    const items = readList(UNRECORDED_KEY);
    items.push({
      label: item.label || "A meal",
      occurred_at: (item.body && item.body.occurred_at) || null,
      reason: reason || "",
    });
    writeList(UNRECORDED_KEY, items);
    renderUnrecorded();
  }

  function describeUnrecorded(item) {
    const when = Date.parse(item.occurred_at || "");
    const stamp = isNaN(when)
      ? "time unknown"
      : new Date(when).toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
    return `${item.label} · ${stamp}`;
  }

  function renderUnrecorded() {
    const items = readList(UNRECORDED_KEY);
    const badge = el("unrecordedBadge");
    if (badge) {
      badge.hidden = items.length === 0;
      badge.textContent =
        items.length === 1 ? "1 scan not recorded" : `${items.length} scans not recorded`;
    }
    const list = el("unrecordedList");
    if (!list) return;
    list.innerHTML = "";
    items.forEach((item) => {
      const row = document.createElement("li");
      row.className = "muted-row";
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = describeUnrecorded(item);
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.reason || "";
      row.append(who, tag);
      list.appendChild(row);
    });
  }

  // Looked up lazily rather than held in a const: readerOwnsKeyboard() reaches
  // this during setup, before a const declared down here would be initialized.
  function unrecordedModalIsOpen() {
    const modal = el("unrecordedModal");
    return Boolean(modal) && !modal.hidden;
  }

  function openUnrecordedModal() {
    if (guestModalIsOpen()) closeGuestModal();
    if (alumniModalIsOpen()) closeAlumniModal();
    // Same rule as the other two popups: what is behind this must not time out
    // and vanish while somebody is reading it.
    if (resultTimer) {
      clearTimeout(resultTimer);
      resultTimer = null;
    }
    renderUnrecorded();
    el("unrecordedModal").hidden = false;
    releaseReader();
  }

  function closeUnrecordedModal() {
    el("unrecordedModal").hidden = true;
    if (readerOwnsKeyboard()) claimReader();
  }

  el("unrecordedBadge").addEventListener("click", openUnrecordedModal);

  el("unrecordedModal").addEventListener("click", (event) => {
    const action = event.target.dataset.action;
    if (action === "cancel") {
      closeUnrecordedModal();
    } else if (action === "clear") {
      // Deliberately not offered until they have been read: the button sits
      // under the list and says what clearing it means.
      writeList(UNRECORDED_KEY, []);
      renderUnrecorded();
      closeUnrecordedModal();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && unrecordedModalIsOpen()) closeUnrecordedModal();
  });

  /* ---------------- scanning ---------------- */

  async function submitScan(value, credentialType, attach) {
    const payload = {
      value: value,
      credential_type: credentialType,
      occurred_at: new Date().toISOString(),
    };
    // Only ever set by the "check in anyway" buttons, and only ever honoured by
    // the server when nothing is being served — see services/scan.py.
    if (attach) payload.attach = attach;
    // Remembered so those buttons can re-submit this scan without the member
    // having to tap their card a second time.
    lastScan = { value: value, credentialType: credentialType };
    try {
      const result = await post("/api/scan", payload);
      renderResult(result);
      flushQueue();
    } catch (err) {
      if (err.status) {
        // The server answered and refused — not a network problem.
        renderError(err.message);
        return;
      }
      enqueue("/api/scan", payload, describeScan(payload));
      renderQueued();
    }
  }

  function bandClassFor(outcome) {
    switch (outcome) {
      case "checked_in":
      case "guest_recorded":
      case "alumni_recorded":
        return "ok";
      case "checked_in_overage":
      case "no_meal_plan":
      case "already_checked_in":
        return "over";
      case "unknown_credential":
      case "guest_quota_exceeded":
        return "stop";
      default:
        return "info";
    }
  }

  function setFallbackInitials(text) {
    el("resultPhoto").hidden = true;
    const fallback = el("photoFallback");
    fallback.hidden = false;
    fallback.firstElementChild.textContent = text;
  }

  // The class year is its own element beside the name rather than part of the
  // same string, so it can be styled down without shrinking the name — and so
  // every other path through this screen has to say what it wants there.
  function setResultName(name, classYear) {
    el("resultName").textContent = name;
    el("resultClassYear").textContent = classYear || "";
  }

  function initialsOf(name) {
    return name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0].toUpperCase())
      .join("");
  }

  function renderResult(result) {
    lastResult = result;
    const member = result.member;
    // An alumni meal has no member at all, so it answers "whose name goes on
    // the screen" itself. Without this the result would fall through to the
    // no-member case and tell an alum their card was not recognized.
    const alumni = result.alumni || null;

    if (member && member.photo_url) {
      const photo = el("resultPhoto");
      photo.src = member.photo_url;
      photo.hidden = false;
      el("photoFallback").hidden = true;
    } else if (member) {
      setFallbackInitials((member.first_name[0] || "?") + (member.last_name[0] || ""));
    } else {
      setFallbackInitials(alumni ? initialsOf(alumni.full_name) || "?" : "?");
    }

    const classYear = member
      ? member.class_year
      : alumni
      ? alumni.class_year
      : null;
    setResultName(
      member ? member.full_name : alumni ? alumni.full_name : "Card not recognized",
      classYear ? "’" + String(classYear).slice(-2) : ""
    );

    const bits = [];
    if (alumni) bits.push("Alumni");
    if (result.period_name) bits.push(result.period_name);
    if (result.weekly && result.weekly.allotment !== null) {
      bits.push(`${result.weekly.used} of ${result.weekly.allotment} this week`);
    }
    if (result.guests) {
      bits.push(
        `${result.guests.remaining} guest meal${result.guests.remaining === 1 ? "" : "s"} left`
      );
    }
    el("resultMeta").textContent = bits.join("  ·  ");
    el("resultWarnings").textContent = (result.warnings || []).join("  ·  ");

    el("resultBand").className = "band " + bandClassFor(result.outcome);
    el("resultMessage").textContent = result.message;

    // An unrecognized card is handed off to the staff enrollment page, with the
    // card value carried across so nobody has to tap twice.
    const unknown = result.outcome === "unknown_credential";
    const enrollLink = el("enrollLink");
    enrollLink.hidden = !unknown;
    if (unknown) {
      const value = result.submitted_value || "";
      enrollLink.href = "/enroll?value=" + encodeURIComponent(value);
    }

    el("guestBtn").hidden = !member;
    el("undoBtn").hidden = !result.attendance_id;

    const offering = renderAnyway(result);

    show("result");
    // Leave an unrecognized card on screen longer: staff have to walk over and
    // read it, which takes more than six seconds. A check-in offered a choice
    // of meals gets the same grace — six seconds is not long enough to read two
    // buttons and decide, and the screen vanishing mid-decision is exactly the
    // trip back to the kiosk this feature exists to save.
    const seconds =
      unknown || offering ? 20 : result.result_seconds || CONFIG.resultSeconds;
    resultTimer = setTimeout(backToIdle, seconds * 1000);

    // A second tap inside one meal period is how a member signs a guest in:
    // their own meal is already recorded, so the tap can only mean the person
    // standing next to them. Open the popup with the member already filled in
    // rather than making them find the button.
    if (result.outcome === "already_checked_in" && member) {
      openGuestModal({ host: member, guests: result.guests });
    }
  }

  function renderError(message) {
    lastResult = null;
    renderAnyway(null);
    setFallbackInitials("!");
    setResultName("Not recorded", "");
    el("resultMeta").textContent = "";
    el("resultWarnings").textContent = "";
    el("resultBand").className = "band stop";
    el("resultMessage").textContent = message;
    el("guestBtn").hidden = true;
    el("undoBtn").hidden = true;
    el("enrollLink").hidden = true;
    show("result");
    resultTimer = setTimeout(backToIdle, CONFIG.resultSeconds * 1000);
  }

  function renderQueued(title) {
    lastResult = null;
    renderAnyway(null);
    setFallbackInitials("…");
    setResultName(title || "Saved offline", "");
    el("resultMeta").textContent = "The server is unreachable right now.";
    el("resultWarnings").textContent = "";
    el("resultBand").className = "band info";
    el("resultMessage").textContent = "Your meal is recorded and will sync automatically.";
    // The one action still available offline. The kiosk never learned who this
    // card belongs to — that is what being offline means — but it does not need
    // to: the card itself names the host, and the server resolves it on replay
    // exactly as it would have at the door. Without this a guest arriving during
    // an outage has no way in at all, since the host search needs the server.
    el("guestBtn").hidden = !lastScan;
    el("undoBtn").hidden = true;
    el("enrollLink").hidden = true;
    show("result");
    writeQueue(readQueue());
    resultTimer = setTimeout(backToIdle, CONFIG.resultSeconds * 1000);
  }

  /* ---------------- check in anyway ----------------
   *
   * A member who is a few minutes early for dinner, or a few minutes late off
   * lunch, is told which meals sit either side of now and can put their
   * check-in against one of them. It is a re-submit of the scan the server has
   * already refused, not a new kind of request: the same value goes back to the
   * same endpoint with the member's choice attached, so the offline queue, the
   * duplicate guard and the weekly counter all behave exactly as they would for
   * a tap inside the window.
   *
   * The buttons are only ever drawn from what the server offered. A club with
   * nothing scheduled before now gets one button, and a kiosk that has been
   * told nothing gets none — never a button naming a meal that does not exist. */

  const ANYWAY_BUTTONS = { previous: el("anywayPrev"), next: el("anywayNext") };

  // Words, not a clock face: "12:30" beside a meal name reads as a time of day,
  // which is the one thing it is not.
  function describeGap(seconds) {
    const whole = Math.max(0, Math.floor(seconds));
    if (whole < 60) return "under a minute";
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    if (!hours) return `${minutes} min`;
    return minutes ? `${hours} hr ${minutes} min` : `${hours} hr`;
  }

  function describeOffer(offer) {
    return offer.direction === "previous"
      ? `ended ${describeGap(offer.seconds_away)} ago`
      : `starts in ${describeGap(offer.seconds_away)}`;
  }

  // Returns whether anything is being offered, which is what tells renderResult
  // to leave the screen up long enough to read it.
  function renderAnyway(result) {
    const offers =
      (result && result.outcome === "outside_service" && result.offers) || [];
    Object.keys(ANYWAY_BUTTONS).forEach((direction) => {
      const button = ANYWAY_BUTTONS[direction];
      const offer = offers.filter((item) => item.direction === direction)[0];
      button.hidden = !offer;
      if (!offer) return;
      button.querySelector(".anyway-meal").textContent = offer.period_name;
      button.querySelector(".anyway-when").textContent = describeOffer(offer);
    });
    el("anywayBox").hidden = offers.length === 0;
    return offers.length > 0;
  }

  el("anywayBox").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-direction]");
    // Nothing to re-send means nothing to do: without the original card value
    // this would post an empty scan.
    if (!button || !lastScan) return;
    submitScan(lastScan.value, lastScan.credentialType, button.dataset.direction);
  });

  /* ---------------- manual ID entry ----------------
   *
   * A PUID is exactly nine decimal digits, and the box is checked twice: the
   * keystroke filter is what a member actually feels, and the check in
   * submitManual is what decides, since a paste, an autofill or a stray
   * setAttribute can all put text in the box without a keystroke ever
   * happening. */

  const PUID_RE = /^[0-9]{9}$/;

  function manualFail(message) {
    const error = el("manualError");
    error.textContent = message;
    error.hidden = false;
    manualInput.focus();
  }

  // Anything that is not a digit never survives being typed, so the member sees
  // the rule rather than being told about it after the fact.
  manualInput.addEventListener("input", () => {
    const cleaned = manualInput.value.replace(/[^0-9]/g, "").slice(0, 9);
    if (cleaned !== manualInput.value) manualInput.value = cleaned;
    if (PUID_RE.test(cleaned)) el("manualError").hidden = true;
  });

  function submitManual() {
    const value = manualInput.value.replace(/[^0-9]/g, "");
    if (!value) {
      manualFail("Enter your Princeton ID number first.");
      return;
    }
    if (!PUID_RE.test(value)) {
      manualFail("A Princeton ID is nine digits, like 905551234.");
      return;
    }
    el("manualError").hidden = true;
    manualInput.value = "";
    submitScan(value, "manual_puid");
  }

  el("manualSubmit").addEventListener("click", submitManual);

  manualInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitManual();
    } else if (event.key === "Escape") {
      backToIdle();
    }
  });

  el("manual").addEventListener("click", (event) => {
    // "Clear" rather than "Cancel" now that the box lives on the idle screen:
    // returning to idle is what empties it, via show().
    if (event.target.dataset.action === "cancel") backToIdle();
  });

  /* ---------------- result actions ---------------- */

  el("doneBtn").addEventListener("click", backToIdle);

  el("undoBtn").addEventListener("click", async () => {
    if (!lastResult || !lastResult.attendance_id) return;
    try {
      await post("/api/undo", { attendance_id: lastResult.attendance_id });
      el("resultBand").className = "band info";
      el("resultMessage").textContent = "Check-in undone.";
      el("undoBtn").hidden = true;
      el("guestBtn").hidden = true;
      resultTimer = setTimeout(backToIdle, 2500);
    } catch (err) {
      renderError(err.message);
    }
  });

  /* ---------------- guest popup ----------------
   *
   * One popup, three ways in: the button at the bottom of the page (no host
   * known yet, so it asks), a member's second tap in a meal period, and the
   * "+ Guest" button on a fresh check-in (both of which already know who the
   * host is and fill them in).
   *
   * A guest is recorded by name *and* NetID — or, for a guest who has none, by
   * a stated reason in its place. The kiosk checks the fields before it posts
   * and the server checks them again; the popup's copy of the rule is there to
   * answer instantly, not to be the rule.
   *
   * The host is held one of two ways, because offline there is no other option.
   * `guestHost` is a member the server resolved. `guestHostCard` is the raw card
   * the host tapped, which is all the kiosk has when that tap was queued rather
   * than answered — the server resolves it on replay, by the same means it would
   * have used at the door. Exactly one of the two is ever set. */

  const guestModal = el("guestModal");
  const guestHostQuery = el("guestHostQuery");
  let guestHost = null; // { id, full_name, puid }
  let guestHostCard = null; // { value, credentialType }
  let guestSearchTimer = null;

  function guestModalIsOpen() {
    return !guestModal.hidden;
  }

  function setGuestError(message) {
    const box = el("guestError");
    box.textContent = message || "";
    box.hidden = !message;
  }

  function renderGuestQuota(guests) {
    const quota = el("guestQuota");
    if (!guests) {
      // Opened from the bottom button before a host was picked: how many guest
      // meals they have left is not knowable yet, and guessing would be worse
      // than saying nothing. The server answers on submit.
      quota.textContent = "";
      el("guestOverride").hidden = true;
      return;
    }
    quota.textContent = `${guests.used} of ${guests.quota} guest meals used this month.`;
    el("guestOverride").hidden = guests.remaining > 0;
  }

  function clearGuestHostResults() {
    el("guestHostResults").innerHTML = "";
  }

  /* ---- "guest has no NetID" ---- */

  const guestNoNetid = el("guestNoNetid");

  function renderGuestNetidFields() {
    const none = guestNoNetid.checked;
    const netid = el("guestNetid");
    const reason = el("guestNetidReason");
    // Both fields stay put and one of them is always dead: disabling rather
    // than hiding makes it obvious what the tick box just did, and stops the
    // popup from resizing under somebody who is mid-type.
    netid.disabled = none;
    reason.disabled = !none;
    if (none) netid.value = "";
    if (!none) reason.value = "";
  }

  guestNoNetid.addEventListener("change", () => {
    setGuestError("");
    renderGuestNetidFields();
    (guestNoNetid.checked ? el("guestNetidReason") : el("guestNetid")).focus();
  });

  function setGuestHost(member, guests) {
    guestHost = member || null;
    if (guestHost) guestHostCard = null;
    renderGuestHost();
    renderGuestQuota(guests || null);
  }

  /* The offline host: a card, with nobody's name attached to it yet. Named
   * rather than searched for, because the search endpoint is the one thing that
   * definitely is not answering. */
  function setGuestHostCard(scan) {
    guestHostCard = scan ? { value: scan.value, credentialType: scan.credentialType } : null;
    if (guestHostCard) guestHost = null;
    renderGuestHost();
    // The quota lives on the server and the server is not talking. Saying
    // nothing is the honest answer; guessing would be worse, and the meal is
    // checked against the real quota when it replays.
    renderGuestQuota(null);
  }

  function renderGuestHost() {
    const known = Boolean(guestHost || guestHostCard);
    el("guestHostChosen").hidden = !known;
    guestHostQuery.hidden = known;
    el("guestHostResults").hidden = known;
    if (!known) return;
    clearGuestHostResults();
    if (guestHost) {
      el("guestHostName").textContent = guestHost.full_name;
      el("guestHostPuid").textContent = "PUID " + guestHost.puid;
    } else {
      // No name to show — the tap that would have produced one is sitting in the
      // queue. The card is what staff can check against the person in front of
      // them, so the card is what it says.
      el("guestHostName").textContent = "The card just tapped";
      el("guestHostPuid").textContent = guestHostCard.value;
    }
  }

  function openGuestModal(options) {
    const opts = options || {};
    // The two popups are alternatives, never a stack: this meal is either a
    // guest's or an alum's.
    if (alumniModalIsOpen()) closeAlumniModal();
    // Whatever is on screen behind the popup must not time out and vanish while
    // somebody is still typing a guest's name into it.
    if (resultTimer) {
      clearTimeout(resultTimer);
      resultTimer = null;
    }
    [
      "guestFirstName",
      "guestLastName",
      "guestNetid",
      "guestNetidReason",
      "guestPin",
      "guestReason",
    ].forEach((id) => {
      el(id).value = "";
    });
    // The next guest is a different person: never inherit the last one's
    // "no NetID" tick.
    guestNoNetid.checked = false;
    renderGuestNetidFields();
    guestHostQuery.value = "";
    clearGuestHostResults();
    setGuestError("");
    guestModal.hidden = false;
    if (opts.hostCard) {
      setGuestHostCard(opts.hostCard);
    } else {
      setGuestHost(opts.host || null, opts.guests || null);
    }
    // The popup is all typing, so the reader gives up the keyboard for as long
    // as it is open.
    releaseReader();
    (guestHost || guestHostCard ? el("guestFirstName") : guestHostQuery).focus();
  }

  function closeGuestModal() {
    guestModal.hidden = true;
    guestHost = null;
    guestHostCard = null;
    clearGuestHostResults();
    setGuestError("");
    if (guestSearchTimer) {
      clearTimeout(guestSearchTimer);
      guestSearchTimer = null;
    }
    if (readerOwnsKeyboard()) claimReader();
  }

  function dismissGuestModal() {
    closeGuestModal();
    backToIdle();
  }

  /* ---- host lookup, for the popup opened from the bottom button ---- */

  async function searchGuestHosts(term) {
    const list = el("guestHostResults");
    if (term.length < 2) {
      clearGuestHostResults();
      return;
    }
    let members = [];
    try {
      const response = await fetch("/api/members/search?q=" + encodeURIComponent(term));
      members = await response.json();
    } catch (err) {
      // The way through this is on the idle screen, not in here: a host who taps
      // their own card gets queued, and the "+ Guest" button on that result
      // opens this popup with the card already standing in for the member.
      setGuestError(
        "Could not search members — the server is unreachable. " +
          "Ask the host to tap their card, then use “+ Guest”."
      );
      return;
    }
    list.innerHTML = "";
    if (!members.length) {
      const empty = document.createElement("li");
      empty.className = "muted-row";
      empty.textContent = "No member matches that.";
      list.appendChild(empty);
      return;
    }
    members.forEach((member) => {
      const row = document.createElement("li");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = member.full_name;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = member.puid;
      row.append(who, tag);
      row.addEventListener("click", () => {
        setGuestError("");
        // Quota is unknown for a member picked this way — the server decides.
        setGuestHost(member, null);
        el("guestFirstName").focus();
      });
      list.appendChild(row);
    });
  }

  guestHostQuery.addEventListener("input", () => {
    const term = guestHostQuery.value.trim();
    if (guestSearchTimer) clearTimeout(guestSearchTimer);
    guestSearchTimer = setTimeout(() => searchGuestHosts(term), 200);
  });

  el("guestHostClear").addEventListener("click", () => {
    setGuestHost(null, null);
    guestHostQuery.focus();
  });

  /* ---- the three ways the popup opens ---- */

  el("guestMealBtn").addEventListener("click", () => openGuestModal({}));

  el("guestBtn").addEventListener("click", () => {
    if (lastResult && lastResult.member) {
      openGuestModal({ host: lastResult.member, guests: lastResult.guests });
    } else if (lastScan) {
      // Offline: the check-in behind this screen is still in the queue, so there
      // is no member to name — but the card that produced it is the same thing
      // the server would have resolved, and it can resolve it on replay.
      openGuestModal({ hostCard: lastScan });
    }
  });

  /* ---- closing and submitting ---- */

  guestModal.addEventListener("click", (event) => {
    if (event.target.dataset.action === "cancel") dismissGuestModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && guestModalIsOpen()) dismissGuestModal();
  });

  el("guestSubmit").addEventListener("click", async () => {
    if (!guestHost && !guestHostCard) {
      setGuestError("Choose the member hosting this guest.");
      guestHostQuery.focus();
      return;
    }
    const first = el("guestFirstName").value.trim();
    const last = el("guestLastName").value.trim();
    const netid = el("guestNetid").value.trim();
    const netidReason = el("guestNetidReason").value.trim();
    if (!first || !last) {
      setGuestError("Enter the guest's first and last name.");
      el(first ? "guestLastName" : "guestFirstName").focus();
      return;
    }
    if (guestNoNetid.checked) {
      if (!netidReason) {
        setGuestError("Say why this guest has no NetID.");
        el("guestNetidReason").focus();
        return;
      }
    } else if (!netid) {
      setGuestError(
        "Enter the guest's Princeton NetID, or tick “Guest has no NetID”."
      );
      el("guestNetid").focus();
      return;
    }

    const needsOverride = !el("guestOverride").hidden;
    const body = {
      guest_first_name: first,
      guest_last_name: last,
      guest_netid: guestNoNetid.checked ? "" : netid,
      guest_netid_reason: guestNoNetid.checked ? netidReason : "",
      occurred_at: new Date().toISOString(),
    };
    // Exactly one of the two, never both: the route reads member_id first, and
    // sending a stale card alongside it would be a second opinion nobody asked
    // for.
    if (guestHost) {
      body.member_id = guestHost.id;
    } else {
      body.host_value = guestHostCard.value;
      body.host_credential_type = guestHostCard.credentialType;
    }
    if (needsOverride) {
      body.staff_pin = el("guestPin").value;
      body.override_reason = el("guestReason").value;
    }

    try {
      const result = await post("/api/guest", body);
      if (result.outcome === "guest_quota_exceeded") {
        // Stay in the popup: the staff PIN is the next thing to happen, and
        // everything already typed is still correct.
        el("guestOverride").hidden = false;
        setGuestError(result.message);
        return;
      }
      closeGuestModal();
      renderResult(result);
    } catch (err) {
      if (err.status) {
        // The server answered and refused. Stay put — everything typed is still
        // correct apart from the one thing it named.
        setGuestError(err.message);
        return;
      }
      // Unreachable. The guest is standing there and the host has already been
      // identified, so the meal is taken now and settled later.
      enqueue("/api/guest", body, describeGuest(body));
      closeGuestModal();
      renderQueued("Guest saved offline");
    }
  });

  /* ---------------- alumni popup ----------------
   *
   * The guest popup's shape without the host: an alum is on nobody's account,
   * so there is no member to search for and no quota to spend. What it does
   * insist on is a way to reach them — an email address or a phone number,
   * either one — because unlike a member or a guest there is no card, PUID or
   * NetID to recover that from once they have walked away.
   *
   * The kiosk checks the fields before it posts and the server checks them
   * again; this copy of the rule exists to answer instantly, not to be the
   * rule. */

  const alumniModal = el("alumniModal");
  const ALUMNI_FIELDS = [
    "alumniFirstName",
    "alumniLastName",
    "alumniClassYear",
    "alumniEmail",
    "alumniPhone",
    "alumniNetid",
  ];

  function alumniModalIsOpen() {
    return !alumniModal.hidden;
  }

  function setAlumniError(message) {
    const box = el("alumniError");
    box.textContent = message || "";
    box.hidden = !message;
  }

  function openAlumniModal() {
    if (guestModalIsOpen()) closeGuestModal();
    // Whatever is behind the popup must not time out and vanish while somebody
    // is still typing into it — same rule as the guest popup.
    if (resultTimer) {
      clearTimeout(resultTimer);
      resultTimer = null;
    }
    ALUMNI_FIELDS.forEach((id) => {
      el(id).value = "";
    });
    setAlumniError("");
    alumniModal.hidden = false;
    releaseReader();
    el("alumniFirstName").focus();
  }

  function closeAlumniModal() {
    alumniModal.hidden = true;
    setAlumniError("");
    if (readerOwnsKeyboard()) claimReader();
  }

  function dismissAlumniModal() {
    closeAlumniModal();
    backToIdle();
  }

  // A class year is four digits and nothing else, filtered as it is typed for
  // the same reason the PUID box is: the member feels the rule rather than
  // being told about it after the fact.
  el("alumniClassYear").addEventListener("input", () => {
    const box = el("alumniClassYear");
    const cleaned = box.value.replace(/[^0-9]/g, "").slice(0, 4);
    if (cleaned !== box.value) box.value = cleaned;
  });

  el("alumniMealBtn").addEventListener("click", openAlumniModal);

  alumniModal.addEventListener("click", (event) => {
    if (event.target.dataset.action === "cancel") dismissAlumniModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && alumniModalIsOpen()) dismissAlumniModal();
  });

  el("alumniSubmit").addEventListener("click", async () => {
    const first = el("alumniFirstName").value.trim();
    const last = el("alumniLastName").value.trim();
    const classYear = el("alumniClassYear").value.replace(/[^0-9]/g, "");
    const email = el("alumniEmail").value.trim();
    const phone = el("alumniPhone").value.trim();
    // Optional, so it is never checked for presence — only normalized, the way
    // every other NetID on this kiosk is.
    const netid = el("alumniNetid").value.replace(/\s/g, "").toLowerCase();

    if (!first || !last) {
      setAlumniError("Enter the alum's first and last name.");
      el(first ? "alumniLastName" : "alumniFirstName").focus();
      return;
    }
    if (classYear.length !== 4) {
      setAlumniError("Enter the alum's class year as four digits, like 2014.");
      el("alumniClassYear").focus();
      return;
    }
    // Either one, never neither. The server says the same thing in the same
    // words, so a member of staff reading the popup and a member of staff
    // reading the error are told the same rule.
    if (!email && !phone) {
      setAlumniError(
        "Enter an email address or a phone number for this alum — either one is enough."
      );
      el("alumniEmail").focus();
      return;
    }

    const body = {
      first_name: first,
      last_name: last,
      class_year: Number(classYear),
      email: email,
      phone: phone,
      netid: netid,
      occurred_at: new Date().toISOString(),
    };

    try {
      const result = await post("/api/alumni", body);
      closeAlumniModal();
      renderResult(result);
    } catch (err) {
      if (err.status) {
        // Stay in the popup: everything typed is still correct apart from the
        // one field the server named.
        setAlumniError(err.message);
        return;
      }
      // Unreachable. An alumni meal is the one that most needs queuing rather
      // than refusing: it is on nobody's account and leaves no card behind, so
      // an alum turned away here is a meal that no later reconciliation could
      // ever reconstruct.
      enqueue("/api/alumni", body, describeAlumni(body));
      closeAlumniModal();
      renderQueued("Alumni meal saved offline");
    }
  });

  /* ---------------- chrome ---------------- */

  function tickClock() {
    el("clock").textContent = new Date().toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  }
  tickClock();
  setInterval(tickClock, 10000);

  /* ---------------- meal banner ----------------
   *
   * The banner answers "can I eat right now, and for how much longer" from
   * across the room. Its countdown is seeded with a *duration* from the server
   * and ticked down locally — never computed from an end timestamp against the
   * laptop's own clock, which on a kiosk is not to be trusted. It re-syncs
   * against the server every minute, so drift cannot accumulate, and the
   * moment it reaches zero it asks the server what is true now rather than
   * guessing that the next window has opened. */

  const banner = {
    serving: CONFIG.serving || false,
    name: CONFIG.periodName || null,
    remaining: CONFIG.secondsRemaining || 0,
    nextName: CONFIG.nextPeriodName || null,
    untilNext: CONFIG.secondsUntilNext || 0,
  };

  function formatRemaining(totalSeconds) {
    const whole = Math.max(0, Math.floor(totalSeconds));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const seconds = whole % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return hours > 0
      ? `${hours}:${pad(minutes)}:${pad(seconds)}`
      : `${minutes}:${pad(seconds)}`;
  }

  function renderBanner() {
    const node = el("mealBanner");
    const countdown = el("mealBannerCountdown");
    if (!node) return;

    if (banner.serving && banner.name) {
      node.dataset.serving = "true";
      el("mealBannerName").textContent = `Now serving ${banner.name}`;
      countdown.hidden = false;
      countdown.textContent = `${formatRemaining(banner.remaining)} left`;
    } else if (banner.nextName) {
      node.dataset.serving = "false";
      el("mealBannerName").textContent = `Next Meal: ${banner.nextName}`;
      countdown.hidden = false;
      countdown.textContent = `in ${formatRemaining(banner.untilNext)}`;
    } else {
      // No schedule at all — there is no next meal to name, so fall back
      // rather than printing "Next Meal: null".
      node.dataset.serving = "false";
      el("mealBannerName").textContent = "Outside Meal Hours";
      countdown.hidden = true;
      countdown.textContent = "";
    }
  }

  function tickBanner() {
    if (banner.serving) {
      banner.remaining -= 1;
      if (banner.remaining <= 0) {
        banner.remaining = 0;
        renderBanner();
        refreshService(); // the window just closed — ask, do not assume
        return;
      }
    } else if (banner.nextName) {
      banner.untilNext -= 1;
      if (banner.untilNext <= 0) {
        banner.untilNext = 0;
        renderBanner();
        refreshService(); // the window just opened — ask, do not assume
        return;
      }
    } else {
      return;
    }
    renderBanner();
  }

  async function refreshService() {
    try {
      const response = await fetch("/api/status");
      const status = await response.json();
      el("serviceLabel").textContent = status.serving
        ? `Serving ${status.period_name} until ${status.period_ends}`
        : "Not currently serving";

      banner.serving = Boolean(status.serving);
      banner.name = status.period_name || null;
      banner.remaining = status.seconds_remaining || 0;
      banner.nextName = status.next_period_name || null;
      banner.untilNext = status.seconds_until_next || 0;
      renderBanner();
    } catch (err) {
      /* leave the last known label and countdown in place */
    }
  }

  renderBanner();
  setInterval(tickBanner, 1000);
  setInterval(refreshService, 60000);

  /* ---------------- PC/SC reader bridge (optional) ----------------
   *
   * A keyboard-wedge reader needs nothing here: it types into the hidden sink
   * and the Enter handler above does the rest. A PC/SC-only reader presents no
   * keyboard at all, so bridge/reader_bridge.py runs on this laptop and offers
   * serials on a loopback WebSocket instead.
   *
   * The bridge is an input path, not a second scanning path: it hands the
   * serial to the same submitScan() a tap would have used, and obeys the same
   * ownership rule as the sink. Without that check a card tapped while the
   * guest popup is open would submit underneath it.
   *
   * Absent bridgeUrl this is inert, so wedge installs are unaffected. */

  function connectBridge() {
    if (!CONFIG.bridgeUrl) return;
    let socket;
    let backoff = 1000;

    const setBadge = (down) => {
      const badge = el("bridgeBadge");
      if (badge) badge.hidden = !down;
    };

    const open = () => {
      try {
        socket = new WebSocket(CONFIG.bridgeUrl);
      } catch (err) {
        // Bad URL — retrying cannot fix it, but the badge must still show,
        // because a reader that silently stops working is how a kiosk dies.
        setBadge(true);
        return;
      }

      socket.addEventListener("open", () => {
        backoff = 1000;
        setBadge(false);
      });

      socket.addEventListener("message", (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        if (!payload || payload.type !== "scan") return;
        const value = String(payload.value || "").trim();
        if (!value || !readerOwnsKeyboard()) return;
        submitScan(value, "csn");
      });

      socket.addEventListener("close", () => {
        setBadge(true);
        setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, 15000);
      });

      // 'error' is always followed by 'close', which owns the retry.
      socket.addEventListener("error", () => setBadge(true));
    };

    open();
  }

  connectBridge();

  migrateLegacyQueue();
  writeQueue(readQueue());
  renderUnrecorded();
  flushQueue();
  focusReader();
})();
