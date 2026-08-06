/* Staff enrollment page.
 *
 * Separate from the kiosk on purpose: enrolling someone is slow, involves a
 * photo, and must not sit on top of the check-in screen while a queue forms.
 *
 * Two modes share the page. "New member" is the default and the common case —
 * the person is not on file, so this form is what creates them, and their
 * details and card are written in one request. "Already in the system" is the
 * replacement-card path: find them, tap the new card, done.
 *
 * The card field is a plain input, so the HID reader can simply be tapped while
 * it has focus — the reader types the value and presses Enter, exactly as it
 * does at the kiosk.
 */
(function () {
  "use strict";

  const el = (id) => document.getElementById(id);

  let mode = "new"; // "new" | "existing"
  let chosenMember = null;
  let capturedPhoto = null;
  let cameraStream = null;

  /* ---------------- formats ---------------- */

  // A TigerCard CSN is 16 hex digits; a PUID is nine decimal digits. Both are
  // checked here so a mis-scan or a typo is caught while the member is still
  // standing at the desk, rather than becoming a card that never scans. The
  // server enforces the same two rules — this is the friendly half.
  const CSN_RE = /^[0-9A-F]{16}$/;
  const PUID_RE = /^[0-9]{9}$/;
  // A NetID is 2–8 letters and digits starting with a letter. Same rule the
  // server applies (app/services/netid.py) and the same one the guest popup
  // checks — this is the friendly half of it.
  const NETID_RE = /^[a-z][a-z0-9]{1,7}$/;

  // Mirrors the server's normalization: readers vary in case and separators,
  // and NetIDs are case-insensitive.
  const normalizeCard = (value) => value.replace(/[\s-]/g, "").toUpperCase();
  const normalizePuid = (value) => value.replace(/[\s-]/g, "");
  const normalizeNetid = (value) => value.replace(/\s/g, "").toLowerCase();

  const CSN_MESSAGE =
    "The card CSN should be 16 hexadecimal digits (0–9, A–F). " +
    "Tap the card again, or check what you typed.";
  const PUID_MESSAGE = "The PUID should be nine digits, like 905559999.";
  const NETID_MESSAGE =
    "A NetID is 2–8 letters and digits starting with a letter, like ak9981.";
  // Four digits is not the whole rule: the server keeps class years inside
  // 1900-2200, so "0987" — nine digits' worth of PUID typed into the wrong box,
  // or a plain slip — is four digits and still refused. Checking the range here
  // too is what turns that into a message beside the field instead of a
  // rejection after submit.
  const CLASS_YEAR_MIN = 1900;
  const CLASS_YEAR_MAX = 2200;
  const CLASS_YEAR_MESSAGE =
    `The class year should be a four-digit year between ${CLASS_YEAR_MIN} ` +
    `and ${CLASS_YEAR_MAX}, like 2028.`;
  const CLASS_YEAR_RE = /^[0-9]{4}$/;

  function classYearOk(value) {
    const trimmed = value.trim();
    if (!trimmed) return true; // blank is a complete answer; the field is optional
    if (!CLASS_YEAR_RE.test(trimmed)) return false;
    const year = Number(trimmed);
    return year >= CLASS_YEAR_MIN && year <= CLASS_YEAR_MAX;
  }

  /* ---------------- live field checking ---------------- */

  /* Both format rules are checked as the field is typed in, not held back
   * until submit: staff at this desk are usually looking at the card, and a
   * short read is much cheaper to fix while it is still in their hand.
   *
   * The message appears a beat after typing stops and disappears the instant
   * the value is right. The delay exists for the reader, which types all 16
   * digits in a burst — showing the error per keystroke would flash a
   * complaint across every good tap. Leaving the field (or submitting) checks
   * immediately, since there is nothing left to wait for.
   */
  const SETTLE_MS = 200;

  function watchField(field, errorBox, rule) {
    let timer = null;

    function hide() {
      clearTimeout(timer);
      errorBox.hidden = true;
      errorBox.textContent = "";
      field.classList.remove("is-invalid");
      field.removeAttribute("aria-invalid");
    }

    function show(message) {
      clearTimeout(timer);
      errorBox.textContent = message;
      errorBox.hidden = false;
      field.classList.add("is-invalid");
      field.setAttribute("aria-invalid", "true");
    }

    function recheck(immediate) {
      clearTimeout(timer);
      // An untouched field is not yet wrong — it is just empty.
      if (rule.ok(field.value) || !field.value.trim()) return hide();
      if (immediate) return show(rule.message);
      timer = setTimeout(() => show(rule.message), SETTLE_MS);
    }

    field.addEventListener("input", () => recheck(false));
    field.addEventListener("blur", () => recheck(true));

    return {
      ok: () => rule.ok(field.value),
      clear: hide,
      flag: (message) => show(message || rule.message),
      /* Used by submit, which also sends staff to the field to be fixed. It
       * carries its own message for the one problem typing cannot show: a
       * field nobody has filled in at all. */
      complain: (message) => {
        show(message || rule.message);
        field.focus();
      },
    };
  }

  /* ---------------- card field ---------------- */

  const cardValue = el("cardValue");

  const cardCheck = watchField(cardValue, el("cardError"), {
    ok: (value) => CSN_RE.test(normalizeCard(value.trim())),
    message: CSN_MESSAGE,
  });

  const puidCheck = watchField(el("puid"), el("puidError"), {
    ok: (value) => PUID_RE.test(normalizePuid(value.trim())),
    message: PUID_MESSAGE,
  });

  const netidCheck = watchField(el("netid"), el("netidError"), {
    ok: (value) => NETID_RE.test(normalizeNetid(value.trim())),
    message: NETID_MESSAGE,
  });

  // The one optional field of the three: blank is a complete answer, so only a
  // year that has been half-typed or is out of range is wrong.
  const classYearCheck = watchField(el("classYear"), el("classYearError"), {
    ok: classYearOk,
    message: CLASS_YEAR_MESSAGE,
  });

  // A card prefilled from the kiosk ("enroll this card") has not been typed
  // here, so check it once on arrival rather than waiting for staff to touch
  // it. Flagged without stealing focus — a prefilled card means the next thing
  // staff want is the member's details, not the field they never touched.
  if (cardValue.value.trim() && !cardCheck.ok()) cardCheck.flag();

  // A tap sends Enter; don't let that submit anything prematurely.
  cardValue.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      el(mode === "new" ? "firstName" : "memberSearch").focus();
    }
  });

  if (!cardValue.value) cardValue.focus();

  /* ---------------- mode switch ---------------- */

  function setMode(next) {
    mode = next;
    const isNew = next === "new";

    el("newMemberFields").hidden = !isNew;
    el("existingFields").hidden = isNew;

    el("modeNewBtn").classList.toggle("is-on", isNew);
    el("modeExistingBtn").classList.toggle("is-on", !isNew);
    el("modeNewBtn").setAttribute("aria-selected", String(isNew));
    el("modeExistingBtn").setAttribute("aria-selected", String(!isNew));

    el("submitBtn").textContent = isNew ? "Enroll member" : "Link card";

    // Whatever was half-filled in the other mode is not what staff meant to
    // send, so clear it rather than carry it silently into the request.
    if (isNew) {
      clearChoice();
    } else {
      clearNewMemberFields();
    }
    setStatus("", null);
  }

  el("modeNewBtn").addEventListener("click", () => setMode("new"));
  el("modeExistingBtn").addEventListener("click", () => setMode("existing"));

  function clearNewMemberFields() {
    ["firstName", "lastName", "puid", "netid", "classYear"].forEach(
      (id) => (el(id).value = "")
    );
    el("planType").value = "plan_19";
    puidCheck.clear();
    netidCheck.clear();
    classYearCheck.clear();
  }

  function clearChoice() {
    chosenMember = null;
    el("chosen").hidden = true;
    el("memberSearch").value = "";
    el("searchResults").innerHTML = "";
  }

  /* ---------------- member search ---------------- */

  let searchTimer = null;

  el("memberSearch").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const term = event.target.value.trim();
    if (term.length < 2) {
      el("searchResults").innerHTML = "";
      return;
    }
    searchTimer = setTimeout(() => runSearch(term), 200);
  });

  async function runSearch(term) {
    const list = el("searchResults");
    try {
      const response = await fetch(`/api/members/search?q=${encodeURIComponent(term)}`);
      const people = await response.json();
      list.innerHTML = "";
      if (!people.length) {
        const empty = document.createElement("li");
        empty.className = "muted-row";
        empty.textContent = "No members match. If they are new, use “New member” above.";
        list.appendChild(empty);
        return;
      }
      people.forEach((person) => {
        const item = document.createElement("li");
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = person.full_name;
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent =
          person.puid +
          (person.netid ? " · " + person.netid : "") +
          (person.class_year ? " · ’" + String(person.class_year).slice(-2) : "") +
          (person.has_card ? " · already has a card" : "");
        item.append(who, tag);
        item.addEventListener("click", () => chooseMember(person));
        list.appendChild(item);
      });
    } catch (err) {
      list.innerHTML = "";
    }
  }

  function chooseMember(person) {
    chosenMember = person;
    el("chosenName").textContent = person.full_name;
    el("chosen").hidden = false;
    el("searchResults").innerHTML = "";
    el("memberSearch").value = "";
    setStatus("", null);
  }

  el("clearChoice").addEventListener("click", () => {
    clearChoice();
    el("memberSearch").focus();
  });

  /* ---------------- photo ---------------- */

  el("startCamBtn").addEventListener("click", startCamera);

  async function startCamera() {
    // getUserMedia only exists in a secure context. On a plain-HTTP LAN origin
    // navigator.mediaDevices is undefined, so say what is actually wrong
    // instead of offering a button that silently does nothing.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showCameraError(
        "Camera unavailable — this page is not a secure context. Relaunch the kiosk " +
          "with run-kiosk.sh, or upload a photo file instead."
      );
      return;
    }
    try {
      cameraStream = await navigator.mediaDevices.getUserMedia({
        video: { width: 640, height: 480, facingMode: "user" },
      });
      el("camera").srcObject = cameraStream;
      el("camera").hidden = false;
      el("cameraError").hidden = true;
      el("startCamBtn").hidden = true;
      el("captureBtn").hidden = false;
    } catch (err) {
      showCameraError("Camera blocked or in use: " + err.message);
    }
  }

  function showCameraError(message) {
    const box = el("cameraError");
    box.textContent = message;
    box.hidden = false;
    el("camera").hidden = true;
    el("captureBtn").hidden = true;
    el("startCamBtn").hidden = true;
  }

  function stopCamera() {
    if (cameraStream) {
      cameraStream.getTracks().forEach((track) => track.stop());
      cameraStream = null;
    }
    el("camera").hidden = true;
  }

  el("captureBtn").addEventListener("click", () => {
    const video = el("camera");
    const canvas = el("snapshot");
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    capturedPhoto = canvas.toDataURL("image/jpeg", 0.9);
    el("preview").src = capturedPhoto;
    el("preview").hidden = false;
    el("captureBtn").hidden = true;
    el("retakeBtn").hidden = false;
    stopCamera();
  });

  el("retakeBtn").addEventListener("click", () => {
    capturedPhoto = null;
    el("preview").hidden = true;
    el("retakeBtn").hidden = true;
    startCamera();
  });

  el("uploadBtn").addEventListener("click", () => el("photoUpload").click());

  el("photoUpload").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      capturedPhoto = reader.result;
      el("preview").src = capturedPhoto;
      el("preview").hidden = false;
      el("cameraError").hidden = true;
    };
    reader.readAsDataURL(file);
  });

  /* ---------------- submit ---------------- */

  function setStatus(message, kind) {
    const box = el("status");
    if (!message) {
      box.hidden = true;
      return;
    }
    box.textContent = message;
    box.className = "status " + (kind || "");
    box.hidden = false;
  }

  /* Returns the request to send, a string describing what is missing, or
   * {inline} naming a field that should complain beside itself instead. */
  function buildRequest(pin, card) {
    if (mode === "existing") {
      if (!chosenMember) return "Search for the member and choose them.";
      return {
        url: "/api/enroll",
        body: {
          member_id: chosenMember.id,
          value: card,
          credential_type: "csn",
          photo_data_url: capturedPhoto,
          staff_pin: pin,
        },
      };
    }

    const firstName = el("firstName").value.trim();
    const lastName = el("lastName").value.trim();
    const puid = normalizePuid(el("puid").value.trim());
    const netid = normalizeNetid(el("netid").value.trim());
    const classYear = el("classYear").value.trim();

    if (!firstName) return "Enter the member's first name.";
    if (!lastName) return "Enter the member's last name.";
    // Reported at the field, in the order the form is filled in.
    if (!puid) return { inline: puidCheck, message: "Enter the member's PUID." };
    if (!puidCheck.ok()) return { inline: puidCheck };
    // Required, unlike the class year below it: every member of this club has a
    // NetID, and the desk is the only place it will ever be asked for.
    if (!netid) return { inline: netidCheck, message: "Enter the member's NetID." };
    if (!netidCheck.ok()) return { inline: netidCheck };
    if (!classYearCheck.ok()) return { inline: classYearCheck };

    return {
      url: "/api/enroll/new",
      body: {
        first_name: firstName,
        last_name: lastName,
        puid: puid,
        netid: netid,
        class_year: classYear ? Number(classYear) : null,
        plan_type: el("planType").value,
        value: card,
        credential_type: "csn",
        photo_data_url: capturedPhoto,
        staff_pin: pin,
      },
    };
  }

  el("submitBtn").addEventListener("click", async () => {
    const pin = el("staffPin").value.trim();
    const card = normalizeCard(cardValue.value.trim());

    if (!pin) return setStatus("Enter the staff PIN.", "bad");

    // The format-checked fields answer for themselves, beside the input, so
    // submitting says nothing new — it only makes sure the complaint is up
    // even if staff never typed in the field, and puts the cursor there.
    if (!cardCheck.ok()) {
      setStatus("", null);
      return cardCheck.complain(card ? undefined : "Tap the card, or type its number.");
    }

    const request = buildRequest(pin, card);
    if (typeof request === "string") return setStatus(request, "bad");
    if (request.inline) {
      setStatus("", null);
      return request.inline.complain(request.message);
    }

    setStatus(mode === "new" ? "Enrolling…" : "Linking…", null);
    try {
      const response = await fetch(request.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request.body),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        // Only a string is printable: anything else would land on the desk as
        // "[object Object]", which tells staff nothing about what to fix.
        const detail = typeof body.detail === "string" ? body.detail : "";
        setStatus(detail || `Could not enroll (${response.status}).`, "bad");
        return;
      }
      stopCamera();
      setStatus(
        `${body.message} Ready for the next one — the form has been cleared.`,
        "good"
      );
      resetForNext();
    } catch (err) {
      setStatus("The server is unreachable. Nothing was saved.", "bad");
    }
  });

  function resetForNext() {
    // Staff enroll people in batches, so keep the PIN and clear everything
    // else rather than making them start the page over each time.
    capturedPhoto = null;
    cardValue.value = "";
    cardCheck.clear();
    clearChoice();
    clearNewMemberFields();
    el("preview").hidden = true;
    el("retakeBtn").hidden = true;
    el("captureBtn").hidden = true;
    el("startCamBtn").hidden = false;
    el("cameraError").hidden = true;
    cardValue.focus();
  }
})();
