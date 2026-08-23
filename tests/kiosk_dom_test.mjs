/* Drives the real kiosk.js against the real rendered kiosk page under jsdom.
 *
 * The kiosk is the one screen members actually touch, and its two input paths
 * (an HID reader that types, and a human that types) compete for the same
 * keyboard. That competition is not testable from Python, so it is tested here.
 *
 * Usage: node kiosk_dom_test.mjs <rendered.html> <kiosk.js>
 * Prints TAP-ish lines and exits non-zero on the first failure.
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, jsPath] = process.argv.slice(2);
const html = readFileSync(htmlPath, "utf8");
const source = readFileSync(jsPath, "utf8");

let failures = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log(`ok - ${name}`);
  } else {
    failures += 1;
    console.log(`FAIL - ${name}`);
    if (detail !== undefined) console.log(`      ${detail}`);
  }
}

const AVERY = {
  id: 1, first_name: "Avery", last_name: "Chen", full_name: "Avery Chen",
  puid: "905550001", class_year: 2028, plan_type: "plan_19", status: "active",
  photo_url: null,
};

/* Returned from `respond` to make the request fail the way an unreachable
 * server does — a rejected fetch, no status — rather than answer at all. */
const OFFLINE = Symbol("offline");

/* `respond(url, body)` may return a payload to answer a request its own way,
 * OFFLINE to fail it, or anything falsy to fall through to the defaults below.
 * `beforeEval(window)` runs after the page exists but before kiosk.js does, for
 * tests that need localStorage already populated or already broken. */
function boot(configOverrides = {}, respond = null, beforeEval = null) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://kiosk.test/" });
  const { window } = dom;

  window.KIOSK_CONFIG = { resultSeconds: 6, undoSeconds: 60, ...configOverrides };
  window.scrollTo = () => {};  // jsdom does not implement it

  const calls = [];
  window.fetch = (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, body });
    const answered = respond ? respond(String(url), body) : null;
    if (answered === OFFLINE) {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    // Every card value is unknown; every PUID resolves. That asymmetry is what
    // makes a mis-routed submit obvious.
    const isScan = String(url).includes("/api/scan");
    const payload =
      answered ||
      (String(url).includes("/api/members/search")
        ? [AVERY]
        : isScan && body && body.credential_type === "manual_puid"
        ? { outcome: "checked_in", ok: true, message: "Checked in — 1 of 19 this week.",
            warnings: [], member: AVERY,
            period_name: "Lunch", weekly: { used: 1, allotment: 19, remaining: 18,
            is_over: false }, guests: { used: 0, quota: 2, remaining: 2 },
            attendance_id: 7, result_seconds: 6 }
        : { outcome: "unknown_credential", ok: false,
            message: "Card not recognized — see staff to enroll.", warnings: [],
            member: null, submitted_value: body ? body.value : null,
            submitted_type: body ? body.credential_type : null, result_seconds: 6 });
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
    });
  };

  if (beforeEval) beforeEval(window);
  window.eval(source);
  return { window, calls, dom };
}

/* A card tapped by somebody who has already eaten this period. */
const alreadyCheckedIn = (url) =>
  url.includes("/api/scan")
    ? { outcome: "already_checked_in", ok: false, message: "Already checked in for Lunch.",
        warnings: [], member: AVERY, period_name: "Lunch",
        weekly: { used: 1, allotment: 19, remaining: 18, is_over: false },
        guests: { used: 0, quota: 2, remaining: 2 }, attendance_id: 7, result_seconds: 6 }
    : null;

const alumniRecorded = (url) =>
  url.includes("/api/alumni")
    ? { outcome: "alumni_recorded", ok: true,
        message: "Alumni meal recorded for Casey Whitman.", warnings: [],
        member: null,
        alumni: { full_name: "Casey Whitman", class_year: 2014,
                  email: "casey@example.com", phone: null, netid: null },
        period_name: "Dinner", attendance_id: 11, result_seconds: 6 }
    : null;

/* A card tapped between meals: refused, but with the windows either side named
 * so the kiosk can offer them. The second scan — the one carrying the member's
 * choice — comes back as an ordinary check-in, which is exactly what the server
 * does once a meal has been attached. */
function betweenMeals(offers = null) {
  const named = offers || [
    { direction: "previous", period_name: "Lunch", service_date: "2026-01-05",
      seconds_away: 1500 },
    { direction: "next", period_name: "Dinner", service_date: "2026-01-05",
      seconds_away: 900 },
  ];
  return (url, body) => {
    if (!url.includes("/api/scan")) return null;
    if (body && body.attach) {
      return { outcome: "checked_in", ok: true,
               message: "Checked in — 1 of 19 this week.",
               warnings: ["Checked in before Dinner opened"], member: AVERY,
               period_name: "Dinner",
               weekly: { used: 1, allotment: 19, remaining: 18, is_over: false },
               guests: { used: 0, quota: 2, remaining: 2 }, offers: [],
               attendance_id: 12, result_seconds: 6 };
    }
    return { outcome: "outside_service", ok: false,
             message: "No meal is being served right now.", warnings: [],
             member: AVERY,
             weekly: { used: 0, allotment: 19, remaining: 19, is_over: false },
             guests: { used: 0, quota: 2, remaining: 2 },
             offers: named, result_seconds: 6 };
  };
}

const guestRecorded = (url) =>
  url.includes("/api/guest")
    ? { outcome: "guest_recorded", ok: true,
        message: "Guest recorded — 1 of 2 guest meals used this month.",
        warnings: [], member: AVERY, period_name: "Lunch",
        guests: { used: 1, quota: 2, remaining: 1 }, attendance_id: 9, result_seconds: 6 }
    : null;

/* Type the way a person actually does: the character lands in whatever input
 * holds focus (as a browser would), and the keydown bubbles from there. */
function typeKey(window, key) {
  const target = window.document.activeElement || window.document.body;
  if (target.tagName === "INPUT" && key.length === 1) {
    target.value += key;
  }
  target.dispatchEvent(
    new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })
  );
}

function typeAll(window, text) {
  for (const ch of text) typeKey(window, ch);
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));

/* ------------------------------------------------------------------ */

/* The ID box sits on the idle screen, under the tap prompt. A member reaches it
 * by clicking straight into it — there is no button in the way — which means the
 * reader watchdog has to leave the box alone the moment it takes focus. */
async function testKeyboardPuidEntry() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  doc.getElementById("manualInput").focus();
  await settle();

  check(
    "clicking into the ID textbox keeps focus against the reader watchdog",
    doc.activeElement === doc.getElementById("manualInput"),
    `activeElement was ${doc.activeElement && doc.activeElement.id}`
  );

  typeAll(window, "905550001");
  await settle();

  check(
    "typed digits land in the ID textbox",
    doc.getElementById("manualInput").value === "905550001",
    `textbox held ${JSON.stringify(doc.getElementById("manualInput").value)}`
  );

  typeKey(window, "Enter");
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check("pressing Enter submits exactly one scan", scans.length === 1,
        `got ${scans.length}: ${JSON.stringify(scans.map((s) => s.body))}`);

  check(
    "the PUID is submitted as a typed ID, not as a card",
    scans.length === 1 && scans[0].body.credential_type === "manual_puid",
    scans.length ? `credential_type was ${scans[0].body.credential_type}` : "no scan sent"
  );

  check(
    "the submitted value is the typed PUID",
    scans.length === 1 && scans[0].body.value === "905550001",
    scans.length ? `value was ${JSON.stringify(scans[0].body.value)}` : "no scan sent"
  );

  check(
    "the member is checked in",
    doc.getElementById("result").hidden === false,
    `result hidden=${doc.getElementById("result").hidden}`
  );

  dom.window.close();
}

async function testEmptySubmitIsRefused() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  doc.getElementById("manualInput").focus();
  await settle();

  doc.getElementById("manualSubmit").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check("an empty ID box submits nothing",
        calls.filter((c) => String(c.url).includes("/api/scan")).length === 0);
  check("an empty ID box explains itself",
        doc.getElementById("manualError").hidden === false);

  dom.window.close();
}

/* A PUID is nine decimal digits and nothing else. The filter is what a member
 * feels while typing; the submit check is what actually decides, because a
 * paste never goes through a keystroke. */
async function testTheIdBoxTakesNineDigitsAndNothingElse() {
  const { window, calls, dom } = boot();
  const doc = window.document;
  const box = doc.getElementById("manualInput");

  const paste = (text) => {
    box.value = text;
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  const submit = () =>
    doc.getElementById("manualSubmit").dispatchEvent(
      new window.MouseEvent("click", { bubbles: true })
    );
  const scans = () => calls.filter((c) => String(c.url).includes("/api/scan"));

  paste("90a-555 1234x");
  check("letters, spaces and dashes never survive being typed",
        box.value === "905551234", `box held ${JSON.stringify(box.value)}`);

  paste("9055512349999");
  check("and the box stops at nine digits",
        box.value === "905551234", `box held ${JSON.stringify(box.value)}`);

  // The attributes the browser itself enforces, so the rule survives a member
  // who reaches the box before kiosk.js has loaded.
  check("the markup states the same rule", box.getAttribute("pattern") === "[0-9]{9}" &&
        box.getAttribute("maxlength") === "9",
        `pattern=${box.getAttribute("pattern")} maxlength=${box.getAttribute("maxlength")}`);

  box.value = "90555";  // short, set past the filter the way a paste would be
  submit();
  await settle();
  check("a short ID is not submitted", scans().length === 0,
        `sent ${JSON.stringify(scans().map((s) => s.body))}`);
  check("and the box says how long a PUID is",
        doc.getElementById("manualError").hidden === false &&
          doc.getElementById("manualError").textContent.includes("nine digits"),
        `error read ${JSON.stringify(doc.getElementById("manualError").textContent)}`);

  box.value = "9055512a4";
  submit();
  await settle();
  check("neither is one with a letter in it", scans().length === 0,
        `sent ${JSON.stringify(scans().map((s) => s.body))}`);

  paste("905550001");
  submit();
  await settle();
  check("a full nine digits goes through", scans().length === 1,
        `got ${scans().length}`);
  check("as the typed PUID, unchanged",
        scans().length === 1 && scans()[0].body.value === "905550001",
        scans().length ? `value was ${JSON.stringify(scans()[0].body.value)}` : "no scan");

  dom.window.close();
}

/* The button that used to open this box is gone; the box is the whole path. */
async function testTheNoCardButtonIsGone() {
  const { window, dom } = boot();
  const doc = window.document;

  check("the idle screen carries no 'No card' button",
        doc.getElementById("noCardBtn") === null);
  check("and kiosk.js still wires up the rest of the page",
        doc.activeElement === doc.getElementById("cardInput"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  dom.window.close();
}

async function testUnknownCardOffersTheEnrollmentPage() {
  const { window, dom } = boot();
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  const link = doc.getElementById("enrollLink");
  check("an unrecognized card offers the staff enrollment page",
        link.hidden === false, `enrollLink hidden=${link.hidden}`);
  check("the enrollment link carries the card value across, so nobody taps twice",
        link.getAttribute("href") === "/enroll?value=A1B2C3D4",
        `href was ${link.getAttribute("href")}`);

  dom.window.close();
}

async function testCardStillWorksOnIdle() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  // A reader tap on the idle screen: characters land in the focused sink,
  // then Enter.
  const sink = doc.getElementById("cardInput");
  check("reader sink holds focus on the idle screen",
        doc.activeElement === sink,
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check("a card tap submits one scan", scans.length === 1);
  check("a card tap is submitted as a card",
        scans.length === 1 && scans[0].body.credential_type === "csn",
        scans.length ? `credential_type was ${scans[0].body.credential_type}` : "no scan");
  check("a card tap sends the card value",
        scans.length === 1 && scans[0].body.value === "A1B2C3D4");

  dom.window.close();
}

async function testStrayDigitsDoNotLeakIntoTheNextScan() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  doc.getElementById("manualInput").focus();
  await settle();
  typeAll(window, "12345");

  // Member gives up and cancels.
  doc.querySelector('#manual [data-action="cancel"]').dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  // Next person taps a card.
  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check(
    "abandoned ID digits do not prepend to the next card scan",
    scans.length === 1 && scans[0].body.value === "A1B2C3D4",
    scans.length ? `value was ${JSON.stringify(scans[0].body.value)}` : "no scan sent"
  );

  dom.window.close();
}

async function testMealBannerCountsDown() {
  const { window, dom } = boot({
    serving: true,
    periodName: "Dinner",
    secondsRemaining: 3725, // 1:02:05
  });
  const doc = window.document;

  check("the banner names the meal being served",
        doc.getElementById("mealBannerName").textContent.trim() === "Now serving Dinner",
        `banner read ${JSON.stringify(doc.getElementById("mealBannerName").textContent)}`);

  const countdown = doc.getElementById("mealBannerCountdown");
  check("the countdown starts from the server's duration, hours included",
        countdown.textContent === "1:02:05 left",
        `countdown read ${JSON.stringify(countdown.textContent)}`);
  check("the banner is styled as serving",
        doc.getElementById("mealBanner").dataset.serving === "true");

  // Two ticks of the one-second interval.
  await new Promise((resolve) => setTimeout(resolve, 2100));
  check("the countdown actually ticks down",
        countdown.textContent === "1:02:03" + " left",
        `countdown read ${JSON.stringify(countdown.textContent)}`);

  dom.window.close();
}

async function testMealBannerDropsHoursUnderAnHour() {
  const { window, dom } = boot({
    serving: true,
    periodName: "Lunch",
    secondsRemaining: 305, // 5:05
  });
  const countdown = window.document.getElementById("mealBannerCountdown");
  check("under an hour the countdown drops the leading zero hour",
        countdown.textContent === "5:05 left",
        `countdown read ${JSON.stringify(countdown.textContent)}`);
  dom.window.close();
}

async function testMealBannerNamesTheNextMeal() {
  const { window, dom } = boot({
    serving: false,
    periodName: null,
    secondsRemaining: 0,
    nextPeriodName: "Breakfast",
    secondsUntilNext: 45015, // 12:30:15
  });
  const doc = window.document;

  check("when closed the banner names the meal that is coming",
        doc.getElementById("mealBannerName").textContent.trim() === "Next Meal: Breakfast",
        `banner read ${JSON.stringify(doc.getElementById("mealBannerName").textContent)}`);

  const countdown = doc.getElementById("mealBannerCountdown");
  check("and how long until it opens",
        countdown.hidden === false && countdown.textContent === "in 12:30:15",
        `countdown read ${JSON.stringify(countdown.textContent)}`);
  check("the banner is still styled as closed",
        doc.getElementById("mealBanner").dataset.serving === "false");

  await new Promise((resolve) => setTimeout(resolve, 2100));
  check("the wait counts down too",
        countdown.textContent === "in 12:30:13",
        `countdown read ${JSON.stringify(countdown.textContent)}`);

  dom.window.close();
}

async function testMealBannerFallsBackWithNoSchedule() {
  const { window, dom } = boot({
    serving: false,
    periodName: null,
    secondsRemaining: 0,
    nextPeriodName: null,
    secondsUntilNext: 0,
  });
  const doc = window.document;

  check("with no next meal to name the banner falls back rather than printing null",
        doc.getElementById("mealBannerName").textContent.trim() === "Outside Meal Hours",
        `banner read ${JSON.stringify(doc.getElementById("mealBannerName").textContent)}`);
  check("and shows no countdown",
        doc.getElementById("mealBannerCountdown").hidden === true);

  dom.window.close();
}

async function testMealBannerAsksTheServerWhenTheNextMealOpens() {
  const { window, calls, dom } = boot({
    serving: false,
    periodName: null,
    secondsRemaining: 0,
    nextPeriodName: "Breakfast",
    secondsUntilNext: 1,
  });

  await new Promise((resolve) => setTimeout(resolve, 1400));

  check("the moment the next meal is due the kiosk asks the server, not itself",
        calls.some((c) => String(c.url).includes("/api/status")),
        `urls seen: ${JSON.stringify(calls.map((c) => c.url))}`);

  dom.window.close();
}

async function testMealBannerAsksTheServerWhenTheWindowCloses() {
  const { window, calls, dom } = boot({
    serving: true,
    periodName: "Dinner",
    secondsRemaining: 1,
  });

  await new Promise((resolve) => setTimeout(resolve, 1400));

  check("hitting zero re-checks with the server rather than assuming",
        calls.some((c) => String(c.url).includes("/api/status")),
        `urls seen: ${JSON.stringify(calls.map((c) => c.url))}`);

  dom.window.close();
}

function click(window, id) {
  window.document
    .getElementById(id)
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

/* ---------------- check in anyway ----------------
 *
 * A member five minutes early for dinner should be able to eat rather than be
 * sent away to come back. The kiosk offers the meals either side of now, and
 * picking one re-submits the scan the server has already refused — so the
 * button has to carry the original card value, which nobody should have to tap
 * a second time. */

async function testBetweenMealsBothNeighbouringMealsAreOffered() {
  const { window, dom } = boot({}, betweenMeals());
  const doc = window.document;

  check("the offer starts hidden", doc.getElementById("anywayBox").hidden === true);

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  check("a tap between meals still says nothing is being served",
        doc.getElementById("resultMessage").textContent ===
          "No meal is being served right now.",
        `band read ${JSON.stringify(doc.getElementById("resultMessage").textContent)}`);
  check("and offers the way out underneath it",
        doc.getElementById("anywayBox").hidden === false);

  const prev = doc.getElementById("anywayPrev");
  const next = doc.getElementById("anywayNext");
  check("both meals are named, one per button",
        prev.hidden === false && next.hidden === false &&
          prev.querySelector(".anyway-meal").textContent === "Lunch" &&
          next.querySelector(".anyway-meal").textContent === "Dinner",
        `prev=${prev.textContent} next=${next.textContent}`);
  check("with how far away each one is, in words rather than a clock face",
        prev.querySelector(".anyway-when").textContent === "ended 25 min ago" &&
          next.querySelector(".anyway-when").textContent === "starts in 15 min",
        `prev=${JSON.stringify(prev.querySelector(".anyway-when").textContent)} ` +
          `next=${JSON.stringify(next.querySelector(".anyway-when").textContent)}`);

  dom.window.close();
}

async function testPickingAMealResubmitsTheSameCard() {
  const { window, calls, dom } = boot({}, betweenMeals());
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  click(window, "anywayNext");
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check("picking a meal sends a second scan", scans.length === 2,
        `got ${scans.length}`);
  check("carrying the same card, so nobody taps twice",
        scans.length === 2 && scans[1].body.value === "A1B2C3D4" &&
          scans[1].body.credential_type === "csn",
        scans.length === 2 ? JSON.stringify(scans[1].body) : "no second scan");
  check("and the meal that was picked",
        scans.length === 2 && scans[1].body.attach === "next",
        scans.length === 2 ? `attach was ${scans[1].body.attach}` : "no second scan");
  check("the first scan asked for no meal at all",
        scans.length >= 1 && scans[0].body.attach === undefined,
        scans.length ? JSON.stringify(scans[0].body) : "no scan");
  check("the confirmation replaces the offer rather than sitting under it",
        doc.getElementById("anywayBox").hidden === true &&
          doc.getElementById("resultMessage").textContent ===
            "Checked in — 1 of 19 this week.",
        `band read ${JSON.stringify(doc.getElementById("resultMessage").textContent)}`);
  check("and says which meal was spent, which the screen cannot otherwise show",
        doc.getElementById("resultWarnings").textContent ===
          "Checked in before Dinner opened",
        `warnings read ${JSON.stringify(doc.getElementById("resultWarnings").textContent)}`);

  dom.window.close();
}

/* A club with nothing scheduled on one side gets one button, not one naming a
 * meal that does not exist. */
async function testOnlyTheMealsTheServerOffersGetButtons() {
  const { window, dom } = boot({}, betweenMeals([
    { direction: "next", period_name: "Breakfast", service_date: "2026-01-06",
      seconds_away: 45015 },
  ]));
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  check("the offered meal gets its button",
        doc.getElementById("anywayNext").hidden === false &&
          doc.getElementById("anywayNext").querySelector(".anyway-meal").textContent ===
            "Breakfast");
  check("the meal that was not offered gets none",
        doc.getElementById("anywayPrev").hidden === true);
  check("and a wait of half a day reads in hours",
        doc.getElementById("anywayNext").querySelector(".anyway-when").textContent ===
          "starts in 12 hr 30 min",
        `read ${JSON.stringify(doc.getElementById("anywayNext")
          .querySelector(".anyway-when").textContent)}`);

  dom.window.close();
}

async function testAnUnrecognizedCardIsOfferedNothing() {
  const { window, dom } = boot();  // every card comes back unknown
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  check("a card the club does not know is offered no meal to check into",
        doc.getElementById("anywayBox").hidden === true);

  dom.window.close();
}

/* Six seconds is not long enough to read two buttons and decide, and the screen
 * vanishing mid-decision is the trip back to the kiosk this feature exists to
 * save. */
async function testTheOfferOutlivesTheUsualResultTimeout() {
  const { window, dom } = boot({ resultSeconds: 1 }, betweenMeals());
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await new Promise((resolve) => setTimeout(resolve, 1400));

  check("the offer is still on screen after the usual timeout",
        doc.getElementById("anywayBox").hidden === false &&
          doc.getElementById("result").hidden === false);

  dom.window.close();
}

/* ---------------- guest meal popup ---------------- */

async function testGuestButtonOpensThePopupAsksWhoIsHosting() {
  const { window, dom } = boot();
  const doc = window.document;

  check("the guest popup starts closed", doc.getElementById("guestModal").hidden === true);

  click(window, "guestMealBtn");
  await settle();

  check("the Guest Meal button opens the popup",
        doc.getElementById("guestModal").hidden === false);
  check("with nobody tapped in, the popup asks which member is hosting",
        doc.getElementById("guestHostQuery").hidden === false &&
          doc.getElementById("guestHostChosen").hidden === true);
  check("and puts the cursor in the member box",
        doc.activeElement === doc.getElementById("guestHostQuery"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);
  check("the idle screen is still underneath, unchanged",
        doc.getElementById("idle").hidden === false);

  dom.window.close();
}

async function testTheHostCanBeSearchedForByName() {
  const { window, dom } = boot();
  const doc = window.document;

  click(window, "guestMealBtn");
  await settle();

  const box = doc.getElementById("guestHostQuery");
  box.value = "Avery";
  box.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 350));

  const rows = doc.querySelectorAll("#guestHostResults li");
  check("searching names the members who match", rows.length === 1,
        `got ${rows.length} rows`);

  rows[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();

  check("picking one fills the host in",
        doc.getElementById("guestHostName").textContent === "Avery Chen" &&
          doc.getElementById("guestHostChosen").hidden === false);
  check("and moves on to the guest's name",
        doc.activeElement === doc.getElementById("guestFirstName"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  dom.window.close();
}

async function testSecondTapInAPeriodOpensThePopupPrefilled() {
  const { window, dom } = boot({}, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  check("a second tap in one meal period opens the guest popup",
        doc.getElementById("guestModal").hidden === false);
  check("the member who tapped is already filled in",
        doc.getElementById("guestHostName").textContent === "Avery Chen",
        `host read ${JSON.stringify(doc.getElementById("guestHostName").textContent)}`);
  check("along with their PUID",
        doc.getElementById("guestHostPuid").textContent.includes("905550001"),
        `puid read ${JSON.stringify(doc.getElementById("guestHostPuid").textContent)}`);
  check("so the popup asks for the guest, not for the member again",
        doc.getElementById("guestHostQuery").hidden === true &&
          doc.activeElement === doc.getElementById("guestFirstName"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);
  check("their remaining guest meals are shown",
        doc.getElementById("guestQuota").textContent ===
          "0 of 2 guest meals used this month.",
        `quota read ${JSON.stringify(doc.getElementById("guestQuota").textContent)}`);

  dom.window.close();
}

async function testTheResultBehindThePopupDoesNotTimeOut() {
  // The result screen normally clears itself after a few seconds. It must not
  // do that while somebody is typing a guest's details over the top of it.
  const { window, dom } = boot({ resultSeconds: 1 }, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await new Promise((resolve) => setTimeout(resolve, 1400));

  check("the popup survives the result screen's timeout",
        doc.getElementById("guestModal").hidden === false);
  check("and so does the result behind it",
        doc.getElementById("result").hidden === false);

  dom.window.close();
}

async function testThePopupHoldsTheKeyboardAgainstTheReaderWatchdog() {
  const { window, dom } = boot({}, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  // Longer than the watchdog's 1.5s sweep, which owns the keyboard on the
  // result screen this popup is sitting on top of.
  await new Promise((resolve) => setTimeout(resolve, 1700));

  check("the reader watchdog does not steal focus back while the popup is open",
        doc.activeElement !== doc.getElementById("cardInput"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  dom.window.close();
}

async function testGuestSubmitSendsTheNameAndNetid() {
  const { window, calls, dom } = boot({}, (url, body) =>
    alreadyCheckedIn(url) || guestRecorded(url)
  );
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  doc.getElementById("guestFirstName").value = "Kim";
  doc.getElementById("guestLastName").value = "Adeyemi";
  doc.getElementById("guestNetid").value = "kadeyemi";
  click(window, "guestSubmit");
  await settle();

  const guests = calls.filter((c) => String(c.url).includes("/api/guest"));
  check("submitting posts exactly one guest meal", guests.length === 1,
        `got ${guests.length}`);
  check("the guest is posted with both name parts and their NetID",
        guests.length === 1 &&
          guests[0].body.guest_first_name === "Kim" &&
          guests[0].body.guest_last_name === "Adeyemi" &&
          guests[0].body.guest_netid === "kadeyemi",
        guests.length ? JSON.stringify(guests[0].body) : "no post sent");
  check("against the member who tapped",
        guests.length === 1 && guests[0].body.member_id === 1,
        guests.length ? `member_id was ${guests[0].body.member_id}` : "no post sent");
  check("and the popup closes onto the confirmation",
        doc.getElementById("guestModal").hidden === true &&
          doc.getElementById("result").hidden === false);

  dom.window.close();
}

async function testGuestSubmitRefusesAnIncompleteGuest() {
  const { window, calls, dom } = boot({}, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  doc.getElementById("guestFirstName").value = "Kim";
  click(window, "guestSubmit");
  await settle();

  check("a guest with no last name is not posted",
        calls.filter((c) => String(c.url).includes("/api/guest")).length === 0);
  check("and the popup says so", doc.getElementById("guestError").hidden === false);

  doc.getElementById("guestLastName").value = "Adeyemi";
  click(window, "guestSubmit");
  await settle();

  check("a guest with no NetID and no tick in the box is not posted either",
        calls.filter((c) => String(c.url).includes("/api/guest")).length === 0);
  check("and the popup asks for one",
        doc.getElementById("guestError").textContent.includes("NetID"),
        `error read ${JSON.stringify(doc.getElementById("guestError").textContent)}`);

  dom.window.close();
}

/* A guest with no NetID: the field cannot simply be skipped, but a visiting
 * parent must not be stuck at the door either. Ticking the box swaps the NetID
 * for a reason, and the reason is required in its place. */

function tick(window, id) {
  const box = window.document.getElementById(id);
  box.checked = true;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
}

async function testNoNetidBoxSwapsTheFieldForAReason() {
  const { window, dom } = boot({}, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  check("the reason box cannot be typed into until the tick box says so",
        doc.getElementById("guestNetidReason").disabled === true);
  check("and the NetID field is the one in use",
        doc.getElementById("guestNetid").disabled === false);

  doc.getElementById("guestNetid").value = "kadeyemi";
  tick(window, "guestNoNetid");
  await settle();

  check("ticking 'no NetID' opens the reason box for typing",
        doc.getElementById("guestNetidReason").disabled === false);
  check("the NetID field is taken out of use",
        doc.getElementById("guestNetid").disabled === true);
  check("and whatever was typed into it is cleared, not left to be posted",
        doc.getElementById("guestNetid").value === "",
        `NetID box held ${JSON.stringify(doc.getElementById("guestNetid").value)}`);
  check("the cursor moves to the reason",
        doc.activeElement === doc.getElementById("guestNetidReason"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  const box = doc.getElementById("guestNoNetid");
  doc.getElementById("guestNetidReason").value = "visiting parent";
  box.checked = false;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await settle();

  check("un-ticking clears the reason, so a sealed box can never post one",
        doc.getElementById("guestNetidReason").value === "",
        `reason held ${JSON.stringify(doc.getElementById("guestNetidReason").value)}`);
  check("un-ticking gives the NetID field back and seals the reason box again",
        doc.getElementById("guestNetid").disabled === false &&
          doc.getElementById("guestNetidReason").disabled === true);

  dom.window.close();
}

async function testNoNetidRequiresAReasonBeforeItWillPost() {
  const { window, calls, dom } = boot({}, (url) =>
    alreadyCheckedIn(url) || guestRecorded(url)
  );
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  doc.getElementById("guestFirstName").value = "Pat";
  doc.getElementById("guestLastName").value = "Ortiz";
  tick(window, "guestNoNetid");
  click(window, "guestSubmit");
  await settle();

  check("a ticked box with no reason posts nothing",
        calls.filter((c) => String(c.url).includes("/api/guest")).length === 0);
  check("and the popup asks for the reason",
        doc.getElementById("guestError").hidden === false &&
          doc.getElementById("guestError").textContent.includes("why"),
        `error read ${JSON.stringify(doc.getElementById("guestError").textContent)}`);

  doc.getElementById("guestNetidReason").value = "visiting parent";
  click(window, "guestSubmit");
  await settle();

  const guests = calls.filter((c) => String(c.url).includes("/api/guest"));
  check("with a reason the guest meal posts", guests.length === 1, `got ${guests.length}`);
  check("carrying the reason and no NetID",
        guests.length === 1 &&
          guests[0].body.guest_netid === "" &&
          guests[0].body.guest_netid_reason === "visiting parent",
        guests.length ? JSON.stringify(guests[0].body) : "no post sent");
  check("and the popup closes onto the confirmation",
        doc.getElementById("guestModal").hidden === true);

  dom.window.close();
}

async function testTheNoNetidTickDoesNotOutliveTheGuest() {
  const { window, dom } = boot({}, (url) => alreadyCheckedIn(url) || guestRecorded(url));
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();
  doc.getElementById("guestFirstName").value = "Pat";
  doc.getElementById("guestLastName").value = "Ortiz";
  tick(window, "guestNoNetid");
  doc.getElementById("guestNetidReason").value = "visiting parent";
  click(window, "guestSubmit");
  await settle();

  click(window, "guestMealBtn");
  await settle();

  check("the next guest starts with the box un-ticked",
        doc.getElementById("guestNoNetid").checked === false);
  check("their NetID field is back in use and the reason box is sealed",
        doc.getElementById("guestNetid").disabled === false &&
          doc.getElementById("guestNetidReason").disabled === true);
  check("and the previous guest's reason is gone",
        doc.getElementById("guestNetidReason").value === "",
        `reason held ${JSON.stringify(doc.getElementById("guestNetidReason").value)}`);

  dom.window.close();
}

async function testCancellingThePopupReturnsToIdleWithNothingLeftBehind() {
  const { window, calls, dom } = boot({}, alreadyCheckedIn);
  const doc = window.document;

  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  doc.getElementById("guestFirstName").value = "Kim";
  doc.querySelector('#guestModal [data-action="cancel"]').dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check("cancelling closes the popup", doc.getElementById("guestModal").hidden === true);
  check("and returns the kiosk to idle", doc.getElementById("idle").hidden === false);
  check("the reader has the keyboard back",
        doc.activeElement === doc.getElementById("cardInput"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  // The next person taps: nothing the previous guest half-typed may follow it.
  typeAll(window, "A1B2C3D4");
  typeKey(window, "Enter");
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check("the next card tap is submitted clean",
        scans.length === 2 && scans[1].body.value === "A1B2C3D4",
        scans.length ? `value was ${JSON.stringify(scans[scans.length - 1].body.value)}` : "none");

  click(window, "guestMealBtn");
  await settle();
  check("and reopening the popup starts from a blank guest",
        doc.getElementById("guestFirstName").value === "");

  dom.window.close();
}

/* ---------------- alumni meal popup ----------------
 *
 * An alum is on nobody's account, so this popup asks for no host and spends no
 * quota. What it will not let through is an alum nobody can be reached from:
 * there is no card, PUID or NetID to recover a contact detail from afterwards.
 */

async function testAlumniButtonOpensThePopup() {
  const { window, dom } = boot();
  const doc = window.document;

  check("the alumni popup starts closed",
        doc.getElementById("alumniModal").hidden === true);

  click(window, "alumniMealBtn");
  await settle();

  check("the Alumni Meal button opens the popup",
        doc.getElementById("alumniModal").hidden === false);
  check("it asks for no host — an alum is hosted by nobody",
        doc.getElementById("alumniModal").querySelector("#guestHostQuery") === null);
  check("and puts the cursor in the alum's first name",
        doc.activeElement === doc.getElementById("alumniFirstName"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);
  check("the idle screen is still underneath, unchanged",
        doc.getElementById("idle").hidden === false);

  dom.window.close();
}

async function testAlumniSubmitSendsTheWholeAlum() {
  const { window, calls, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();

  doc.getElementById("alumniFirstName").value = "Casey";
  doc.getElementById("alumniLastName").value = "Whitman";
  doc.getElementById("alumniClassYear").value = "2014";
  doc.getElementById("alumniEmail").value = "casey@example.com";
  click(window, "alumniSubmit");
  await settle();

  const posts = calls.filter((c) => String(c.url).includes("/api/alumni"));
  check("submitting posts exactly one alumni meal", posts.length === 1,
        `got ${posts.length}`);
  check("carrying the alum's name, class year and contact detail",
        posts.length === 1 &&
          posts[0].body.first_name === "Casey" &&
          posts[0].body.last_name === "Whitman" &&
          posts[0].body.class_year === 2014 &&
          posts[0].body.email === "casey@example.com",
        posts.length ? JSON.stringify(posts[0].body) : "no post sent");
  check("and no member, because there is none",
        posts.length === 1 && posts[0].body.member_id === undefined,
        posts.length ? JSON.stringify(posts[0].body) : "no post sent");
  check("the popup closes onto the confirmation",
        doc.getElementById("alumniModal").hidden === true &&
          doc.getElementById("result").hidden === false);
  check("which names the alum rather than saying the card was not recognized",
        doc.getElementById("resultName").textContent === "Casey Whitman",
        `result read ${JSON.stringify(doc.getElementById("resultName").textContent)}`);
  check("and shows their class year beside it",
        doc.getElementById("resultClassYear").textContent === "’14",
        `class year read ${JSON.stringify(doc.getElementById("resultClassYear").textContent)}`);

  dom.window.close();
}

/* An alum's NetID is asked for but never insisted on: many keep one for life,
 * plenty have let theirs lapse, and it reaches nobody either way. */
async function testTheAlumniNetidIsOptional() {
  const { window, calls, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();

  check("the popup marks the NetID as optional, unlike everything above it",
        doc.querySelector('label[for="alumniNetid"]').textContent.includes("optional"),
        `label read ${JSON.stringify(doc.querySelector('label[for="alumniNetid"]').textContent)}`);

  doc.getElementById("alumniFirstName").value = "Casey";
  doc.getElementById("alumniLastName").value = "Whitman";
  doc.getElementById("alumniClassYear").value = "2014";
  doc.getElementById("alumniEmail").value = "casey@example.com";
  // NetID left blank.
  click(window, "alumniSubmit");
  await settle();

  const posts = calls.filter((c) => String(c.url).includes("/api/alumni"));
  check("an alum with no NetID is posted all the same", posts.length === 1,
        `got ${posts.length}`);
  check("with an empty NetID, not a missing field",
        posts.length === 1 && posts[0].body.netid === "",
        posts.length ? JSON.stringify(posts[0].body) : "no post sent");

  dom.window.close();
}

async function testTheAlumniNetidIsSentNormalized() {
  const { window, calls, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();

  doc.getElementById("alumniFirstName").value = "Casey";
  doc.getElementById("alumniLastName").value = "Whitman";
  doc.getElementById("alumniClassYear").value = "2014";
  doc.getElementById("alumniEmail").value = "casey@example.com";
  doc.getElementById("alumniNetid").value = " CWhitman ";
  click(window, "alumniSubmit");
  await settle();

  const posts = calls.filter((c) => String(c.url).includes("/api/alumni"));
  check("a NetID typed in capitals is sent lowercase, like every other one here",
        posts.length === 1 && posts[0].body.netid === "cwhitman",
        posts.length ? JSON.stringify(posts[0].body.netid) : "no post sent");

  dom.window.close();
}

async function testAPhoneNumberAloneIsEnough() {
  const { window, calls, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();

  doc.getElementById("alumniFirstName").value = "Casey";
  doc.getElementById("alumniLastName").value = "Whitman";
  doc.getElementById("alumniClassYear").value = "2014";
  doc.getElementById("alumniPhone").value = "609-555-1234";
  click(window, "alumniSubmit");
  await settle();

  const posts = calls.filter((c) => String(c.url).includes("/api/alumni"));
  check("an alum who gives only a phone number is posted", posts.length === 1,
        `got ${posts.length}`);
  check("with the phone number and an empty email",
        posts.length === 1 &&
          posts[0].body.phone === "609-555-1234" &&
          posts[0].body.email === "",
        posts.length ? JSON.stringify(posts[0].body) : "no post sent");

  dom.window.close();
}

async function testAlumniSubmitRefusesAnIncompleteAlum() {
  const { window, calls, dom } = boot({}, alumniRecorded);
  const doc = window.document;
  const posts = () => calls.filter((c) => String(c.url).includes("/api/alumni"));
  const error = () => doc.getElementById("alumniError");

  click(window, "alumniMealBtn");
  await settle();

  doc.getElementById("alumniFirstName").value = "Casey";
  click(window, "alumniSubmit");
  await settle();
  check("an alum with no last name is not posted", posts().length === 0);
  check("and the popup says so", error().hidden === false);

  doc.getElementById("alumniLastName").value = "Whitman";
  click(window, "alumniSubmit");
  await settle();
  check("an alum with no class year is not posted either", posts().length === 0);
  check("and the popup asks for it",
        error().textContent.includes("class year"),
        `error read ${JSON.stringify(error().textContent)}`);

  doc.getElementById("alumniClassYear").value = "2014";
  click(window, "alumniSubmit");
  await settle();
  check("nor is one with no way to reach them", posts().length === 0);
  check("and the popup says either contact detail will do",
        error().textContent.includes("email address or a phone number"),
        `error read ${JSON.stringify(error().textContent)}`);

  doc.getElementById("alumniEmail").value = "casey@example.com";
  click(window, "alumniSubmit");
  await settle();
  check("with one contact detail the meal posts", posts().length === 1,
        `got ${posts().length}`);

  dom.window.close();
}

async function testTheClassYearBoxTakesFourDigitsAndNothingElse() {
  const { window, dom } = boot();
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();

  const box = doc.getElementById("alumniClassYear");
  box.value = "'14 class of 20144";
  box.dispatchEvent(new window.Event("input", { bubbles: true }));

  check("letters and punctuation never survive being typed, and it stops at four",
        box.value === "1420", `box held ${JSON.stringify(box.value)}`);
  check("the markup states the same rule",
        box.getAttribute("pattern") === "[0-9]{4}" &&
          box.getAttribute("maxlength") === "4",
        `pattern=${box.getAttribute("pattern")} maxlength=${box.getAttribute("maxlength")}`);

  dom.window.close();
}

async function testTheAlumniPopupHoldsTheKeyboard() {
  const { window, dom } = boot();
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();
  doc.getElementById("alumniFirstName").focus();

  // Longer than the watchdog's 1.5s sweep, which owns the keyboard on the idle
  // screen this popup is sitting on top of.
  await new Promise((resolve) => setTimeout(resolve, 1700));

  check("the reader watchdog does not steal focus while the alumni popup is open",
        doc.activeElement !== doc.getElementById("cardInput"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  dom.window.close();
}

async function testCancellingTheAlumniPopupLeavesNothingBehind() {
  const { window, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "alumniMealBtn");
  await settle();
  doc.getElementById("alumniFirstName").value = "Casey";
  doc.querySelector('#alumniModal [data-action="cancel"]').dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check("cancelling closes the popup", doc.getElementById("alumniModal").hidden === true);
  check("and returns the kiosk to idle", doc.getElementById("idle").hidden === false);
  check("the reader has the keyboard back",
        doc.activeElement === doc.getElementById("cardInput"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  click(window, "alumniMealBtn");
  await settle();
  check("and reopening starts from a blank alum",
        doc.getElementById("alumniFirstName").value === "" &&
          doc.getElementById("alumniEmail").value === "");

  dom.window.close();
}

/* The two popups are alternatives, never a stack: this meal is either a guest's
 * or an alum's. A guest's half-typed name left live under an alumni form is the
 * kind of thing nobody notices until the export is wrong. */
async function testTheTwoPopupsAreAlternatives() {
  const { window, dom } = boot({}, alumniRecorded);
  const doc = window.document;

  click(window, "guestMealBtn");
  await settle();
  doc.getElementById("guestFirstName").value = "Kim";

  click(window, "alumniMealBtn");
  await settle();
  check("opening the alumni popup closes the guest popup",
        doc.getElementById("alumniModal").hidden === false &&
          doc.getElementById("guestModal").hidden === true);
  check("and the cursor is in the alumni form, not the one that just closed",
        doc.activeElement === doc.getElementById("alumniFirstName"),
        `activeElement was ${doc.activeElement && doc.activeElement.id}`);

  click(window, "guestMealBtn");
  await settle();
  check("and the other way round",
        doc.getElementById("guestModal").hidden === false &&
          doc.getElementById("alumniModal").hidden === true);
  check("with the abandoned guest's name gone",
        doc.getElementById("guestFirstName").value === "",
        `first name held ${JSON.stringify(doc.getElementById("guestFirstName").value)}`);

  dom.window.close();
}

await testMealBannerCountsDown();
await testMealBannerDropsHoursUnderAnHour();
await testMealBannerNamesTheNextMeal();
await testMealBannerFallsBackWithNoSchedule();
await testMealBannerAsksTheServerWhenTheWindowCloses();
await testMealBannerAsksTheServerWhenTheNextMealOpens();
await testKeyboardPuidEntry();
await testEmptySubmitIsRefused();
await testTheIdBoxTakesNineDigitsAndNothingElse();
await testTheNoCardButtonIsGone();
await testUnknownCardOffersTheEnrollmentPage();
await testCardStillWorksOnIdle();
await testStrayDigitsDoNotLeakIntoTheNextScan();
await testBetweenMealsBothNeighbouringMealsAreOffered();
await testPickingAMealResubmitsTheSameCard();
await testOnlyTheMealsTheServerOffersGetButtons();
await testAnUnrecognizedCardIsOfferedNothing();
await testTheOfferOutlivesTheUsualResultTimeout();
await testGuestButtonOpensThePopupAsksWhoIsHosting();
await testTheHostCanBeSearchedForByName();
await testSecondTapInAPeriodOpensThePopupPrefilled();
await testTheResultBehindThePopupDoesNotTimeOut();
await testThePopupHoldsTheKeyboardAgainstTheReaderWatchdog();
await testGuestSubmitSendsTheNameAndNetid();
await testGuestSubmitRefusesAnIncompleteGuest();
await testNoNetidBoxSwapsTheFieldForAReason();
await testNoNetidRequiresAReasonBeforeItWillPost();
await testTheNoNetidTickDoesNotOutliveTheGuest();
await testCancellingThePopupReturnsToIdleWithNothingLeftBehind();
await testAlumniButtonOpensThePopup();
await testAlumniSubmitSendsTheWholeAlum();
await testTheAlumniNetidIsOptional();
await testTheAlumniNetidIsSentNormalized();
await testAPhoneNumberAloneIsEnough();
await testAlumniSubmitRefusesAnIncompleteAlum();
await testTheClassYearBoxTakesFourDigitsAndNothingElse();
await testTheAlumniPopupHoldsTheKeyboard();
await testCancellingTheAlumniPopupLeavesNothingBehind();
await testTheTwoPopupsAreAlternatives();

/* ---------------- the offline queue ----------------
 *
 * The queue is the only part of the kiosk that runs without the server
 * watching, so anything it gets wrong is invisible by construction. What these
 * pin down is the one rule that makes it safe: a scan leaves the queue when a
 * row exists for it, and never on any weaker evidence. */

const QUEUE_KEY = "colomeal.queue.v2";
const LEGACY_QUEUE_KEY = "colomeal.queue.v1";
const UNRECORDED_KEY = "colomeal.unrecorded.v1";

/* A queue entry as kiosk.js stores one: where it was going, what it was, and
 * what to call it if it ends up in front of staff. */
const queued = (path, body, label) => ({ path: path, body: body, label: label });

const scanBody = (value, at, type = "csn") => ({
  value: value,
  credential_type: type,
  occurred_at: at,
});

const stored = (window, key) => JSON.parse(window.localStorage.getItem(key) || "[]");

async function tapCard(window, value) {
  window.document.getElementById("cardInput").focus();
  typeAll(window, value);
  typeKey(window, "Enter");
  await settle();
}

/* A 200 is not a receipt. unknown_credential, outside_service and
 * guest_quota_exceeded are all answered 200 with no row written, and the queue
 * used to treat "the server replied" as "the meal was recorded" — so a card
 * tapped during an outage that turned out not to be enrolled was dropped on the
 * floor with nothing left behind at all. */
async function testAReplayTheServerDeclinesIsKeptForStaff() {
  let offline = true;
  const { window } = boot({}, (url) =>
    offline && url.includes("/api/scan") ? OFFLINE : null
  );

  await tapCard(window, "04A1B2C3D4E5F601");

  check(
    "a scan taken while the server is unreachable is queued",
    stored(window, QUEUE_KEY).length === 1,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );

  // The server comes back — and refuses the card, with a 200 and no row.
  offline = false;
  window.dispatchEvent(new window.Event("online"));
  await settle();

  check(
    "a declined replay does not stay in the queue retrying forever",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );

  const unrecorded = stored(window, UNRECORDED_KEY);
  check(
    "a declined replay is kept as a meal that was never recorded",
    unrecorded.length === 1,
    `unrecorded held ${JSON.stringify(unrecorded)}`
  );
  check(
    "the kept scan carries the card value staff need to act on it",
    unrecorded.length === 1 && /04A1B2C3D4E5F601/.test(unrecorded[0].label || ""),
    `label was ${unrecorded.length ? JSON.stringify(unrecorded[0].label) : "absent"}`
  );
  check(
    "the kept scan says why the server would not take it",
    unrecorded.length === 1 && /not recognized/i.test(unrecorded[0].reason || ""),
    `reason was ${unrecorded.length ? JSON.stringify(unrecorded[0].reason) : "absent"}`
  );

  const badge = window.document.getElementById("unrecordedBadge");
  check(
    "the badge shows staff there is something to deal with",
    badge.hidden === false && /1 scan/.test(badge.textContent),
    `badge hidden=${badge.hidden} text=${JSON.stringify(badge.textContent)}`
  );
}

/* The mirror image: a replay that really was recorded leaves nothing behind.
 * Without this the fix above would just be a different way to cry wolf. */
async function testAReplayThatIsRecordedLeavesNothingBehind() {
  let offline = true;
  const { window } = boot({}, (url) =>
    offline && url.includes("/api/scan") ? OFFLINE : null
  );

  // A typed PUID, which the harness's server answers with a real attendance_id.
  window.document.getElementById("manualInput").focus();
  typeAll(window, "905550001");
  typeKey(window, "Enter");
  await settle();

  check(
    "the typed ID is queued while the server is unreachable",
    stored(window, QUEUE_KEY).length === 1,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );

  offline = false;
  window.dispatchEvent(new window.Event("online"));
  await settle();

  check(
    "a replay that gets a row leaves the queue",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
  check(
    "a replay that gets a row is not reported as unrecorded",
    stored(window, UNRECORDED_KEY).length === 0,
    `unrecorded held ${JSON.stringify(stored(window, UNRECORDED_KEY))}`
  );
  check(
    "the badge stays hidden when nothing was missed",
    window.document.getElementById("unrecordedBadge").hidden === true,
    "badge was visible"
  );
}

/* A laptop closed on Friday night and reopened on Monday would replay Friday's
 * dinner, the server would refuse to believe a timestamp that old and file it
 * under Monday instead — wrong service date, wrong meal week, wrong allotment,
 * and no sign anything had happened. The kiosk holds the same 24h rule so it
 * stops sending them. */
async function testAScanTooOldToFileCorrectlyIsNeverSent() {
  const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
  const { window, calls } = boot({}, null, (w) => {
    w.localStorage.setItem(
      QUEUE_KEY,
      JSON.stringify([
        queued("/api/scan", scanBody("04A1B2C3D4E5F601", threeDaysAgo), "Card 04A1B2C3D4E5F601"),
      ])
    );
  });
  await settle();

  check(
    "a scan older than the replay window is never sent",
    calls.filter((c) => String(c.url).includes("/api/scan")).length === 0,
    `sent ${JSON.stringify(calls.map((c) => c.url))}`
  );
  check(
    "it leaves the queue rather than being retried forever",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );

  const unrecorded = stored(window, UNRECORDED_KEY);
  check(
    "it is reported as a meal that needs entering by hand",
    unrecorded.length === 1 && /too old/i.test(unrecorded[0].reason || ""),
    `unrecorded held ${JSON.stringify(unrecorded)}`
  );
}

/* A scan still inside the window is exactly what the queue is for, so the age
 * check must not swallow ordinary replays. */
async function testARecentQueuedScanIsStillReplayed() {
  const anHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const { calls } = boot({}, null, (w) => {
    w.localStorage.setItem(
      QUEUE_KEY,
      JSON.stringify([
        queued(
          "/api/scan",
          scanBody("905550001", anHourAgo, "manual_puid"),
          "ID 905550001"
        ),
      ])
    );
  });
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check(
    "an hour-old queued scan is replayed normally",
    scans.length === 1 && scans[0].body.occurred_at === anHourAgo,
    `sent ${JSON.stringify(scans.map((s) => s.body))}`
  );
}

/* localStorage throws in a private-browsing window and when the disk is full.
 * The throw came out of enqueue(), escaped submitScan's own catch and left the
 * member looking at the previous screen — no answer, from the one code path
 * whose whole job is to answer when the server cannot. */
async function testAFullDiskStillAnswersTheMember() {
  const breakStorage = (w) => {
    // Assigning to the Storage instance does not take under jsdom — the
    // prototype is what the page actually calls through.
    Object.getPrototypeOf(w.localStorage).setItem = () => {
      throw new Error("QuotaExceededError");
    };
  };

  // The first write happens during setup, so an unguarded one does not merely
  // break the queue — it throws out of kiosk.js as it loads and takes the whole
  // kiosk with it, reader and all.
  let booted;
  try {
    booted = boot({}, (url) => (url.includes("/api/scan") ? OFFLINE : null), breakStorage);
  } catch (err) {
    check("a kiosk that cannot write to disk still starts up", false, String(err));
    return;
  }
  check("a kiosk that cannot write to disk still starts up", true);

  const { window } = booted;
  await tapCard(window, "04A1B2C3D4E5F601");

  const doc = window.document;
  check(
    "a kiosk that cannot write to disk still shows the member a result",
    doc.getElementById("result").hidden === false,
    "the result screen never appeared"
  );
  check(
    "and still tells them the meal was taken offline",
    /saved offline/i.test(doc.getElementById("resultName").textContent),
    `name read ${JSON.stringify(doc.getElementById("resultName").textContent)}`
  );
}

/* Three things start a flush — the 20s timer, the online event and every
 * successful scan — so two really can overlap. Both would read the same queue,
 * send everything twice, and the slower one would write back a `remaining` that
 * resurrected what the faster had just cleared. */
async function testOverlappingFlushesDoNotSendTheSameScanTwice() {
  const anHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const { window, calls } = boot({}, null, (w) => {
    w.localStorage.setItem(
      QUEUE_KEY,
      JSON.stringify([
        queued(
          "/api/scan",
          scanBody("905550001", anHourAgo, "manual_puid"),
          "ID 905550001"
        ),
      ])
    );
  });

  // Fire a second and third flush while the first is still in flight.
  window.dispatchEvent(new window.Event("online"));
  window.dispatchEvent(new window.Event("online"));
  await settle();

  check(
    "a queued scan is sent once however many flushes race",
    calls.filter((c) => String(c.url).includes("/api/scan")).length === 1,
    `sent ${calls.filter((c) => String(c.url).includes("/api/scan")).length}`
  );
  check(
    "and the queue is left empty, not resurrected by the slower flush",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
}

/* The badge is only useful if it leads somewhere, and the panel it opens has to
 * obey the same keyboard rule the other two popups do — a card tapped while
 * staff are reading it must not submit underneath them. */
async function testTheUnrecordedPanelOpensAndHoldsTheKeyboard() {
  let offline = true;
  const { window, calls } = boot({}, (url) =>
    offline && url.includes("/api/scan") ? OFFLINE : null
  );
  const doc = window.document;

  await tapCard(window, "04A1B2C3D4E5F601");
  offline = false;
  window.dispatchEvent(new window.Event("online"));
  await settle();

  doc.getElementById("unrecordedBadge").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check(
    "clicking the badge opens the panel",
    doc.getElementById("unrecordedModal").hidden === false,
    "the panel stayed hidden"
  );
  check(
    "the panel lists the scan that was missed",
    /04A1B2C3D4E5F601/.test(doc.getElementById("unrecordedList").textContent),
    `list read ${JSON.stringify(doc.getElementById("unrecordedList").textContent)}`
  );
  check(
    "the reader does not hold the keyboard while the panel is open",
    doc.activeElement !== doc.getElementById("cardInput"),
    "the hidden reader sink still had focus"
  );

  const before = calls.filter((c) => String(c.url).includes("/api/scan")).length;
  typeAll(window, "04A1B2C3D4E5F602");
  typeKey(window, "Enter");
  await settle();
  check(
    "a card tapped while the panel is open does not submit underneath it",
    calls.filter((c) => String(c.url).includes("/api/scan")).length === before,
    "a scan was submitted from behind the panel"
  );

  // Clearing is the only way out of the badge, so it has to actually work.
  doc
    .querySelector('#unrecordedModal [data-action="clear"]')
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();

  check(
    "clearing the list puts the badge away",
    doc.getElementById("unrecordedBadge").hidden === true &&
      stored(window, UNRECORDED_KEY).length === 0,
    `badge hidden=${doc.getElementById("unrecordedBadge").hidden}`
  );
  check(
    "and hands the keyboard back to the reader",
    doc.activeElement === doc.getElementById("cardInput"),
    `activeElement was ${doc.activeElement && doc.activeElement.id}`
  );
}

/* An alumni meal is the one that most needs queuing: it is on nobody's account
 * and leaves no card behind, so an alum turned away during an outage is a meal
 * no later reconciliation could reconstruct. */
async function testAnAlumniMealTakenOfflineIsQueued() {
  let offline = true;
  const { window, calls } = boot({}, (url) =>
    offline && url.includes("/api/alumni") ? OFFLINE : null
  );
  const doc = window.document;

  doc.getElementById("alumniMealBtn").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();
  doc.getElementById("alumniFirstName").value = "Casey";
  doc.getElementById("alumniLastName").value = "Whitman";
  doc.getElementById("alumniClassYear").value = "2014";
  doc.getElementById("alumniEmail").value = "casey@example.com";
  doc.getElementById("alumniSubmit").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  const items = stored(window, QUEUE_KEY);
  check(
    "an alumni meal taken while the server is unreachable is queued",
    items.length === 1 && items[0].path === "/api/alumni",
    `queue held ${JSON.stringify(items)}`
  );
  check(
    "it carries the whole alum, not just a name",
    items.length === 1 &&
      items[0].body.class_year === 2014 &&
      items[0].body.email === "casey@example.com",
    `body was ${items.length ? JSON.stringify(items[0].body) : "absent"}`
  );
  check(
    "it stamps the time it was actually eaten",
    items.length === 1 && typeof items[0].body.occurred_at === "string",
    `occurred_at was ${items.length ? JSON.stringify(items[0].body.occurred_at) : "absent"}`
  );
  check(
    "the popup closes and says the meal was taken",
    doc.getElementById("alumniModal").hidden === true &&
      /alumni meal saved offline/i.test(doc.getElementById("resultName").textContent),
    `name read ${JSON.stringify(doc.getElementById("resultName").textContent)}`
  );

  offline = false;
  window.dispatchEvent(new window.Event("online"));
  await settle();

  const replayed = calls.filter((c) => String(c.url).includes("/api/alumni"));
  check(
    "and it is replayed to the alumni endpoint on reconnect",
    replayed.length === 2 && replayed[1].body.first_name === "Casey",
    `alumni calls: ${JSON.stringify(replayed.map((c) => c.body && c.body.first_name))}`
  );
  check(
    "leaving the queue empty",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
}

/* A guest meal offline has a problem an alumni meal does not: the host. The
 * member search needs the server, and the host's own tap was queued rather than
 * answered, so there is no member_id to be had. The card is what stands in. */
async function testAGuestMealTakenOfflineIsHostedByTheCard() {
  let offline = true;
  const { window, calls } = boot({}, (url) => (offline ? OFFLINE : null));
  const doc = window.document;

  // The host taps their own card — queued, so no member ever comes back.
  await tapCard(window, "04A1B2C3D4E5F601");

  check(
    "the offline result still offers the guest button",
    doc.getElementById("guestBtn").hidden === false,
    "there was no way to sign a guest in"
  );

  doc.getElementById("guestBtn").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check(
    "the popup opens with the tapped card standing in for the host",
    doc.getElementById("guestHostChosen").hidden === false &&
      /04A1B2C3D4E5F601/.test(doc.getElementById("guestHostPuid").textContent),
    `host read ${JSON.stringify(doc.getElementById("guestHostPuid").textContent)}`
  );

  doc.getElementById("guestFirstName").value = "Robin";
  doc.getElementById("guestLastName").value = "Ellis";
  doc.getElementById("guestNetid").value = "re1234";
  doc.getElementById("guestSubmit").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  const guests = stored(window, QUEUE_KEY).filter((i) => i.path === "/api/guest");
  check(
    "the guest meal is queued",
    guests.length === 1,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
  check(
    "hosted by the card rather than by a member id it never learned",
    guests.length === 1 &&
      guests[0].body.host_value === "04A1B2C3D4E5F601" &&
      guests[0].body.member_id === undefined,
    `body was ${guests.length ? JSON.stringify(guests[0].body) : "absent"}`
  );
  check(
    "and carries the guest's own details",
    guests.length === 1 &&
      guests[0].body.guest_first_name === "Robin" &&
      guests[0].body.guest_netid === "re1234",
    `body was ${guests.length ? JSON.stringify(guests[0].body) : "absent"}`
  );

  offline = false;
  window.dispatchEvent(new window.Event("online"));
  await settle();

  check(
    "both the check-in and the guest meal replay on reconnect",
    calls.filter((c) => String(c.url).includes("/api/guest")).length === 2 &&
      calls.filter((c) => String(c.url).includes("/api/scan")).length === 2,
    `scan=${calls.filter((c) => String(c.url).includes("/api/scan")).length} ` +
      `guest=${calls.filter((c) => String(c.url).includes("/api/guest")).length}`
  );
}

/* Online, the host is a resolved member and must stay one — the card path is a
 * fallback, not a replacement. */
async function testAnOnlineGuestMealStillUsesTheMemberId() {
  const { window, calls } = boot({}, (url, body) => alreadyCheckedIn(url) || guestRecorded(url));
  const doc = window.document;

  await tapCard(window, "04A1B2C3D4E5F601");
  // The second tap in a period opens the popup with the host filled in.
  doc.getElementById("guestFirstName").value = "Robin";
  doc.getElementById("guestLastName").value = "Ellis";
  doc.getElementById("guestNetid").value = "re1234";
  doc.getElementById("guestSubmit").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  const posted = calls.filter((c) => String(c.url).includes("/api/guest"));
  check(
    "an online guest meal identifies the host by member id",
    posted.length === 1 && posted[0].body.member_id === AVERY.id,
    `body was ${posted.length ? JSON.stringify(posted[0].body) : "absent"}`
  );
  check(
    "and does not also send a card for the server to second-guess it with",
    posted.length === 1 && posted[0].body.host_value === undefined,
    `body was ${posted.length ? JSON.stringify(posted[0].body) : "absent"}`
  );
  check(
    "nothing is queued when the server is answering",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
}

/* A guest or alumni meal the server refuses outright must stay in the popup, not
 * queue — the person is still standing there and can fix what they typed. */
async function testARefusedGuestMealDoesNotQueue() {
  const { window } = boot({}, (url) => {
    if (!url.includes("/api/guest")) return alreadyCheckedIn(url);
    return null;
  });
  const doc = window.document;

  // Answer /api/guest with a refusal rather than a rejection.
  const realFetch = window.fetch;
  window.fetch = (url, options) => {
    if (String(url).includes("/api/guest")) {
      return Promise.resolve({
        ok: false,
        status: 422,
        json: () => Promise.resolve({ detail: "Enter the guest's Princeton NetID." }),
      });
    }
    return realFetch(url, options);
  };

  await tapCard(window, "04A1B2C3D4E5F601");
  doc.getElementById("guestFirstName").value = "Robin";
  doc.getElementById("guestLastName").value = "Ellis";
  doc.getElementById("guestNetid").value = "re1234";
  doc.getElementById("guestSubmit").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check(
    "a refused guest meal is not queued behind the staff's back",
    stored(window, QUEUE_KEY).length === 0,
    `queue held ${JSON.stringify(stored(window, QUEUE_KEY))}`
  );
  check(
    "the popup stays open with the reason on it",
    doc.getElementById("guestModal").hidden === false &&
      /NetID/.test(doc.getElementById("guestError").textContent),
    `error read ${JSON.stringify(doc.getElementById("guestError").textContent)}`
  );
}

/* A kiosk updated in the middle of an outage must not throw away what the
 * previous version had already queued. */
async function testTheOldQueueFormatIsCarriedAcross() {
  const justNow = new Date().toISOString();
  const { window, calls } = boot({}, null, (w) => {
    w.localStorage.setItem(
      LEGACY_QUEUE_KEY,
      JSON.stringify([scanBody("905550001", justNow, "manual_puid")])
    );
  });
  await settle();

  const scans = calls.filter((c) => String(c.url).includes("/api/scan"));
  check(
    "a scan queued by the previous version is still replayed",
    scans.length === 1 && scans[0].body.value === "905550001",
    `sent ${JSON.stringify(scans.map((s) => s.body))}`
  );
  check(
    "and the old key is emptied rather than replayed again next time",
    stored(window, LEGACY_QUEUE_KEY).length === 0,
    `legacy held ${JSON.stringify(stored(window, LEGACY_QUEUE_KEY))}`
  );
}

await testAReplayTheServerDeclinesIsKeptForStaff();
await testAReplayThatIsRecordedLeavesNothingBehind();
await testAScanTooOldToFileCorrectlyIsNeverSent();
await testARecentQueuedScanIsStillReplayed();
await testAFullDiskStillAnswersTheMember();
await testOverlappingFlushesDoNotSendTheSameScanTwice();
await testTheUnrecordedPanelOpensAndHoldsTheKeyboard();
await testAnAlumniMealTakenOfflineIsQueued();
await testAGuestMealTakenOfflineIsHostedByTheCard();
await testAnOnlineGuestMealStillUsesTheMemberId();
await testARefusedGuestMealDoesNotQueue();
await testTheOldQueueFormatIsCarriedAcross();

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
