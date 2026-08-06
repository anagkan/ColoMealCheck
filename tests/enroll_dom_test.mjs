/* Drives the real enroll.js against the real rendered enrollment page under jsdom.
 *
 * The page has two modes that submit to different endpoints with different
 * bodies. Getting that routing wrong is silent and expensive — a new member
 * posted to /api/enroll would 422, and worse, a replacement card posted to
 * /api/enroll/new would try to create a duplicate person. Neither is visible
 * from a Python test of the template, so the routing is tested here for real.
 *
 * Usage: node enroll_dom_test.mjs <rendered.html> <enroll.js>
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

function boot() {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://kiosk.test/" });
  const { window } = dom;

  const calls = [];
  window.fetch = (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url: String(url), body });
    if (String(url).includes("/api/members/search")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve([
            { id: 42, full_name: "Avery Chen", puid: "905550001",
              class_year: 2028, has_card: true },
          ]),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ message: "Done." }),
    });
  };

  window.eval(source);
  return { window, calls, dom };
}

const click = (window, id) =>
  window.document
    .getElementById(id)
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

const setValue = (window, id, value) => {
  const field = window.document.getElementById(id);
  field.value = value;
  field.dispatchEvent(new window.Event("input", { bubbles: true }));
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
// The member search debounces by 200ms before it fetches.
const settleSearch = () => new Promise((resolve) => setTimeout(resolve, 350));
// A field's format complaint waits 200ms after typing stops before it appears.
const settleErrors = () => new Promise((resolve) => setTimeout(resolve, 300));

const errorText = (window, id) => {
  const box = window.document.getElementById(id);
  return box.hidden ? "" : box.textContent;
};

/* ------------------------------------------------------------------ */

async function testNewMemberIsTheDefault() {
  const { window, dom } = boot();
  const doc = window.document;

  check("the new-member fields are the ones showing on arrival",
        doc.getElementById("newMemberFields").hidden === false);
  check("the replacement-card search starts hidden",
        doc.getElementById("existingFields").hidden === true);
  check("the button says what it will do",
        doc.getElementById("submitBtn").textContent.trim() === "Enroll member",
        `button read ${JSON.stringify(doc.getElementById("submitBtn").textContent)}`);

  dom.window.close();
}

async function testEnrollingANewMemberSendsEveryField() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");
  setValue(window, "classYear", "2028");
  window.document.getElementById("planType").value = "plan_14";

  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("one enrollment is submitted", posts.length === 1,
        `got ${posts.length}`);
  check("a new member goes to the create-and-link endpoint",
        posts.length === 1 && posts[0].url === "/api/enroll/new",
        posts.length ? `posted to ${posts[0].url}` : "nothing posted");

  const body = posts.length ? posts[0].body : {};
  check("first and last name are sent separately, not as one name",
        body.first_name === "Sam" && body.last_name === "Okafor",
        JSON.stringify({ first: body.first_name, last: body.last_name }));
  check("the class year is sent as a number",
        body.class_year === 2028, `class_year was ${JSON.stringify(body.class_year)}`);
  check("the PUID and card travel together",
        body.puid === "905559999" && body.value === "04A1B2C3D4E5F601",
        JSON.stringify({ puid: body.puid, value: body.value }));
  check("the NetID is sent alongside the PUID",
        body.netid === "sokafor", `netid was ${JSON.stringify(body.netid)}`);
  check("the chosen meal plan is sent", body.plan_type === "plan_14",
        `plan_type was ${body.plan_type}`);

  dom.window.close();
}

async function testTheFormClearsForTheNextPerson() {
  const { window, dom } = boot();
  const doc = window.document;

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");
  setValue(window, "classYear", "2028");

  click(window, "submitBtn");
  await settle();

  check("the staff PIN survives, because staff enroll in batches",
        doc.getElementById("staffPin").value === "1234");
  check("the previous person's details do not carry into the next enrollment",
        doc.getElementById("firstName").value === "" &&
          doc.getElementById("lastName").value === "" &&
          doc.getElementById("puid").value === "" &&
          doc.getElementById("netid").value === "" &&
          doc.getElementById("classYear").value === "" &&
          doc.getElementById("cardValue").value === "",
        "a field still held the last member's value");

  dom.window.close();
}

async function testMissingDetailsAreRefusedBeforeSending() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  // No last name, no PUID.

  click(window, "submitBtn");
  await settle();

  check("an incomplete member is not submitted",
        calls.filter((c) => c.url.includes("/api/enroll")).length === 0);
  check("the page says what is missing",
        window.document.getElementById("status").hidden === false);

  dom.window.close();
}

async function testABadClassYearIsCaught() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");
  setValue(window, "classYear", "28");

  click(window, "submitBtn");
  await settle();

  check("a two-digit class year is refused rather than sent",
        calls.filter((c) => c.url.includes("/api/enroll")).length === 0);
  check("the complaint is at the class year field",
        errorText(window, "classYearError").includes("four-digit"),
        errorText(window, "classYearError") || "(no error shown)");
  check("submitting puts the cursor in the class year field",
        window.document.activeElement === window.document.getElementById("classYear"),
        `focus was on ${window.document.activeElement.id || "(none)"}`);

  dom.window.close();
}

async function testTheClassYearIsCheckedWhileItIsBeingTyped() {
  const { window, calls, dom } = boot();

  setValue(window, "classYear", "20");
  await settleErrors();
  check("a half-typed class year is called out as it is typed",
        errorText(window, "classYearError").includes("four-digit"),
        errorText(window, "classYearError") || "(no error shown)");

  setValue(window, "classYear", "2028");
  check("the fourth digit clears the complaint",
        errorText(window, "classYearError") === "",
        errorText(window, "classYearError"));

  // Four digits is not enough on its own: the server keeps class years inside
  // 1900-2200, and a year it would refuse has to be caught at the field rather
  // than come back as a rejected submit.
  setValue(window, "classYear", "1234");
  await settleErrors();
  check("a four-digit year outside the allowed range is called out too",
        errorText(window, "classYearError").includes("1900"),
        errorText(window, "classYearError") || "(no error shown)");

  // Unlike the card and the PUID, this field is optional: blank is an answer.
  setValue(window, "classYear", "");
  await settleErrors();
  check("an empty class year is not an error",
        errorText(window, "classYearError") === "",
        errorText(window, "classYearError"));

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");
  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("and a member with no class year still enrolls",
        posts.length === 1 && posts[0].body.class_year === null,
        posts.length ? JSON.stringify(posts[0].body.class_year) : "nothing posted");

  dom.window.close();
}

async function testACardThatIsNotACsnIsRefused() {
  // A short read, a stray character, and the reader's own punctuation: the
  // first two must never reach the server, the third must.
  for (const bad of ["04A1B2C3D4E5F6", "04A1B2C3D4E5F60G", "04A1B2C3D4E5F6012"]) {
    const { window, calls, dom } = boot();

    setValue(window, "staffPin", "1234");
    setValue(window, "cardValue", bad);
    setValue(window, "firstName", "Sam");
    setValue(window, "lastName", "Okafor");
    setValue(window, "puid", "905559999");
    setValue(window, "netid", "sokafor");

    click(window, "submitBtn");
    await settle();

    check(`the card ${bad} is not submitted`,
          calls.filter((c) => c.url.includes("/api/enroll")).length === 0);
    check("the complaint is at the card field, not in the submit status",
          errorText(window, "cardError").includes("16 hexadecimal"),
          errorText(window, "cardError") || "(no error shown)");
    check("submitting puts the cursor back in the card field",
          window.document.activeElement === window.document.getElementById("cardValue"),
          `focus was on ${window.document.activeElement.id || "(none)"}`);

    dom.window.close();
  }
}

async function testTheCardIsCheckedWhileItIsBeingTyped() {
  const { window, dom } = boot();

  check("an untouched card field carries no complaint",
        errorText(window, "cardError") === "");

  setValue(window, "cardValue", "04A1B2");
  check("the complaint holds off while the reader is mid-burst",
        errorText(window, "cardError") === "",
        "it appeared before typing had settled");

  await settleErrors();
  check("a partial card is called out without waiting for submit",
        errorText(window, "cardError").includes("16 hexadecimal"),
        errorText(window, "cardError") || "(no error shown)");
  check("the field itself is marked",
        window.document.getElementById("cardValue").classList.contains("is-invalid"));

  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  check("the complaint clears the moment the card is right, with no delay",
        errorText(window, "cardError") === "",
        errorText(window, "cardError"));
  check("the field is no longer marked",
        window.document.getElementById("cardValue").classList.contains("is-invalid") === false);

  // Emptying the field is not an error — it is just an unfilled field again.
  setValue(window, "cardValue", "");
  await settleErrors();
  check("clearing the field clears the complaint rather than restating it",
        errorText(window, "cardError") === "",
        errorText(window, "cardError"));

  dom.window.close();
}

async function testThePuidIsCheckedWhileItIsBeingTyped() {
  const { window, dom } = boot();

  setValue(window, "puid", "9055");
  await settleErrors();
  check("a short PUID is called out as it is typed",
        errorText(window, "puidError").includes("nine digits"),
        errorText(window, "puidError") || "(no error shown)");

  setValue(window, "puid", "905559999");
  check("the ninth digit clears the complaint",
        errorText(window, "puidError") === "",
        errorText(window, "puidError"));

  setValue(window, "puid", "9055599990");
  await settleErrors();
  check("a tenth digit brings it back",
        errorText(window, "puidError").includes("nine digits"));

  dom.window.close();
}

async function testAPrefilledCardIsCheckedOnArrival() {
  // The kiosk sends an unrecognized tap here as ?value=…, so the field can
  // already hold a bad read that nobody has typed into.
  const badPrefill = html.replace('value=""', 'value="04A1B2C3"');
  const dom = new JSDOM(badPrefill, { runScripts: "outside-only", url: "http://kiosk.test/" });
  dom.window.eval(source);

  check("a card prefilled from the kiosk is checked without being touched",
        errorText(dom.window, "cardError").includes("16 hexadecimal"),
        errorText(dom.window, "cardError") || "(no error shown)");
  check("but it does not steal focus from the details staff came here to type",
        dom.window.document.activeElement !==
          dom.window.document.getElementById("cardValue"));

  dom.window.close();
}

async function testACardIsNormalizedBeforeItIsSent() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04a1-b2c3-d4e5-f601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");

  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("a lowercase, dashed read is accepted and sent normalized",
        posts.length === 1 && posts[0].body.value === "04A1B2C3D4E5F601",
        posts.length ? JSON.stringify(posts[0].body.value) : "nothing posted");

  dom.window.close();
}

async function testAPuidThatIsNotNineDigitsIsRefused() {
  for (const bad of ["90555999", "9055599999", "90555999X"]) {
    const { window, calls, dom } = boot();

    setValue(window, "staffPin", "1234");
    setValue(window, "cardValue", "04A1B2C3D4E5F601");
    setValue(window, "firstName", "Sam");
    setValue(window, "lastName", "Okafor");
    setValue(window, "puid", bad);

    click(window, "submitBtn");
    await settle();

    check(`the PUID ${bad} is refused rather than sent`,
          calls.filter((c) => c.url.includes("/api/enroll")).length === 0);
    check("the complaint is at the PUID field, not in the submit status",
          errorText(window, "puidError").includes("nine digits"),
          errorText(window, "puidError") || "(no error shown)");
    check("submitting puts the cursor in the PUID field",
          window.document.activeElement === window.document.getElementById("puid"),
          `focus was on ${window.document.activeElement.id || "(none)"}`);

    dom.window.close();
  }
}

/* The NetID is the second identifier, and unlike the class year beside it there
 * is no such thing as a member without one — so the page will not send an
 * enrollment that leaves it out. */

async function testTheNetidIsCheckedWhileItIsBeingTyped() {
  const { window, dom } = boot();

  check("an untouched NetID field carries no complaint",
        errorText(window, "netidError") === "");

  setValue(window, "netid", "9sam");
  await settleErrors();
  check("a NetID starting with a digit is called out as it is typed",
        errorText(window, "netidError").includes("starting with a letter"),
        errorText(window, "netidError") || "(no error shown)");

  setValue(window, "netid", "sokafor");
  check("a real NetID clears the complaint with no delay",
        errorText(window, "netidError") === "",
        errorText(window, "netidError"));

  setValue(window, "netid", "waytoolongnetid");
  await settleErrors();
  check("and one too long brings it back",
        errorText(window, "netidError").includes("2–8"),
        errorText(window, "netidError") || "(no error shown)");

  dom.window.close();
}

async function testAMemberWithNoNetidIsRefused() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  // No NetID — unlike the class year, blank is not an answer here.

  click(window, "submitBtn");
  await settle();

  check("an enrollment with no NetID is not submitted",
        calls.filter((c) => c.url.includes("/api/enroll")).length === 0);
  check("the complaint is at the NetID field, not in the submit status",
        errorText(window, "netidError").includes("NetID"),
        errorText(window, "netidError") || "(no error shown)");
  check("submitting puts the cursor in the NetID field",
        window.document.activeElement === window.document.getElementById("netid"),
        `focus was on ${window.document.activeElement.id || "(none)"}`);

  dom.window.close();
}

async function testANetidIsNormalizedBeforeItIsSent() {
  const { window, calls, dom } = boot();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", " SOkafor ");

  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("a NetID typed in capitals is accepted and sent lowercase",
        posts.length === 1 && posts[0].body.netid === "sokafor",
        posts.length ? JSON.stringify(posts[0].body.netid) : "nothing posted");

  dom.window.close();
}

async function testReplacementCardUsesTheExistingMemberEndpoint() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  click(window, "modeExistingBtn");
  await settle();

  check("switching modes reveals the search",
        doc.getElementById("existingFields").hidden === false &&
          doc.getElementById("newMemberFields").hidden === true);
  check("the button changes to match the mode",
        doc.getElementById("submitBtn").textContent.trim() === "Link card",
        `button read ${JSON.stringify(doc.getElementById("submitBtn").textContent)}`);

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F602");
  setValue(window, "memberSearch", "Chen");
  await settleSearch();

  doc.querySelector("#searchResults li").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();

  check("the chosen member is confirmed on screen",
        doc.getElementById("chosen").hidden === false &&
          doc.getElementById("chosenName").textContent === "Avery Chen");

  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("a replacement card goes to the link endpoint, not the create endpoint",
        posts.length === 1 && posts[0].url === "/api/enroll",
        posts.length ? `posted to ${posts[0].url}` : "nothing posted");
  check("it links by member id and sends no new-member details",
        posts.length === 1 && posts[0].body.member_id === 42 &&
          posts[0].body.first_name === undefined,
        posts.length ? JSON.stringify(posts[0].body) : "nothing posted");

  dom.window.close();
}

async function testSwitchingModesDropsTheOtherModesInput() {
  const { window, calls, dom } = boot();
  const doc = window.document;

  // Half-fill a new member, then change your mind.
  setValue(window, "firstName", "Sam");
  setValue(window, "puid", "905559999");
  click(window, "modeExistingBtn");
  await settle();
  click(window, "modeNewBtn");
  await settle();

  check("abandoned new-member details do not reappear",
        doc.getElementById("firstName").value === "" &&
          doc.getElementById("puid").value === "",
        "a field kept its abandoned value");

  // And the reverse: a chosen member must not follow you into new-member mode.
  click(window, "modeExistingBtn");
  await settle();
  setValue(window, "memberSearch", "Chen");
  await settleSearch();
  doc.querySelector("#searchResults li").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true })
  );
  await settle();
  click(window, "modeNewBtn");
  await settle();

  setValue(window, "staffPin", "1234");
  setValue(window, "cardValue", "04A1B2C3D4E5F601");
  setValue(window, "firstName", "Sam");
  setValue(window, "lastName", "Okafor");
  setValue(window, "puid", "905559999");
  setValue(window, "netid", "sokafor");
  click(window, "submitBtn");
  await settle();

  const posts = calls.filter((c) => c.url.includes("/api/enroll"));
  check("a previously chosen member does not hijack a new-member enrollment",
        posts.length === 1 && posts[0].url === "/api/enroll/new",
        posts.length ? `posted to ${posts[0].url}` : "nothing posted");

  dom.window.close();
}

await testNewMemberIsTheDefault();
await testEnrollingANewMemberSendsEveryField();
await testTheFormClearsForTheNextPerson();
await testMissingDetailsAreRefusedBeforeSending();
await testABadClassYearIsCaught();
await testTheClassYearIsCheckedWhileItIsBeingTyped();
await testACardThatIsNotACsnIsRefused();
await testTheCardIsCheckedWhileItIsBeingTyped();
await testThePuidIsCheckedWhileItIsBeingTyped();
await testAPrefilledCardIsCheckedOnArrival();
await testACardIsNormalizedBeforeItIsSent();
await testAPuidThatIsNotNineDigitsIsRefused();
await testTheNetidIsCheckedWhileItIsBeingTyped();
await testAMemberWithNoNetidIsRefused();
await testANetidIsNormalizedBeforeItIsSent();
await testReplacementCardUsesTheExistingMemberEndpoint();
await testSwitchingModesDropsTheOtherModesInput();

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
