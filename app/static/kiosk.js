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
  const QUEUE_KEY = "colomeal.queue.v1";

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
    return guestModalIsOpen() || alumniModalIsOpen();
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
    // anything. Closing it here does not loop: neither close function calls show.
    if (guestModalIsOpen()) closeGuestModal();
    if (alumniModalIsOpen()) closeAlumniModal();

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

  /* ---------------- offline queue ---------------- */

  function readQueue() {
    try {
      return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
    } catch (err) {
      return [];
    }
  }

  function writeQueue(items) {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(items));
    const badge = el("offlineBadge");
    if (items.length) {
      badge.hidden = false;
      badge.textContent = `Offline — ${items.length} queued`;
    } else {
      badge.hidden = true;
    }
  }

  function enqueue(payload) {
    const items = readQueue();
    items.push(payload);
    writeQueue(items);
  }

  async function flushQueue() {
    const items = readQueue();
    if (!items.length) return;
    const remaining = [];
    for (const item of items) {
      try {
        await post("/api/scan", item);
      } catch (err) {
        // Still unreachable — keep this and everything after it for next time.
        remaining.push(item);
      }
    }
    writeQueue(remaining);
  }

  setInterval(flushQueue, 20000);
  window.addEventListener("online", flushQueue);

  /* ---------------- scanning ---------------- */

  async function submitScan(value, credentialType) {
    const payload = {
      value: value,
      credential_type: credentialType,
      occurred_at: new Date().toISOString(),
    };
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
      enqueue(payload);
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

    show("result");
    // Leave an unrecognized card on screen longer: staff have to walk over and
    // read it, which takes more than six seconds.
    const seconds = unknown ? 20 : result.result_seconds || CONFIG.resultSeconds;
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

  function renderQueued() {
    lastResult = null;
    setFallbackInitials("…");
    setResultName("Saved offline", "");
    el("resultMeta").textContent = "The server is unreachable right now.";
    el("resultWarnings").textContent = "";
    el("resultBand").className = "band info";
    el("resultMessage").textContent = "Your meal is recorded and will sync automatically.";
    el("guestBtn").hidden = true;
    el("undoBtn").hidden = true;
    el("enrollLink").hidden = true;
    show("result");
    writeQueue(readQueue());
    resultTimer = setTimeout(backToIdle, CONFIG.resultSeconds * 1000);
  }

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
   * answer instantly, not to be the rule. */

  const guestModal = el("guestModal");
  const guestHostQuery = el("guestHostQuery");
  let guestHost = null; // { id, full_name, puid }
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
    const chosen = el("guestHostChosen");
    chosen.hidden = !guestHost;
    guestHostQuery.hidden = Boolean(guestHost);
    el("guestHostResults").hidden = Boolean(guestHost);
    if (guestHost) {
      el("guestHostName").textContent = guestHost.full_name;
      el("guestHostPuid").textContent = "PUID " + guestHost.puid;
      clearGuestHostResults();
    }
    renderGuestQuota(guests || null);
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
    setGuestHost(opts.host || null, opts.guests || null);
    // The popup is all typing, so the reader gives up the keyboard for as long
    // as it is open.
    releaseReader();
    (guestHost ? el("guestFirstName") : guestHostQuery).focus();
  }

  function closeGuestModal() {
    guestModal.hidden = true;
    guestHost = null;
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
      setGuestError("Could not search members — the server is unreachable.");
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
    if (!lastResult || !lastResult.member) return;
    openGuestModal({ host: lastResult.member, guests: lastResult.guests });
  });

  /* ---- closing and submitting ---- */

  guestModal.addEventListener("click", (event) => {
    if (event.target.dataset.action === "cancel") dismissGuestModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && guestModalIsOpen()) dismissGuestModal();
  });

  el("guestSubmit").addEventListener("click", async () => {
    if (!guestHost) {
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
      member_id: guestHost.id,
      guest_first_name: first,
      guest_last_name: last,
      guest_netid: guestNoNetid.checked ? "" : netid,
      guest_netid_reason: guestNoNetid.checked ? netidReason : "",
    };
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
      setGuestError(err.message);
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

    try {
      const result = await post("/api/alumni", {
        first_name: first,
        last_name: last,
        class_year: Number(classYear),
        email: email,
        phone: phone,
        netid: netid,
      });
      closeAlumniModal();
      renderResult(result);
    } catch (err) {
      // Stay in the popup: everything typed is still correct apart from the one
      // field the server named.
      setAlumniError(err.message);
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

  writeQueue(readQueue());
  flushQueue();
  focusReader();
})();
