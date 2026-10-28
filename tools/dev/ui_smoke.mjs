// Drives the web UI in headless Chromium: status, device search, drop a file
// on a device, accept an incoming offer, accept 2 of 3 files, browser
// download, send to a LocalSend receiver that wants a PIN, history after a
// daemon restart. Started by tools/dev/ui_smoke.py.
// Usage: node ui_smoke.mjs URL SCREENSHOT_DIR   (PLAYWRIGHT_CORE=path/to/playwright-core)
import { createRequire } from "module";
import readline from "readline";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE);
const [url, shots] = process.argv.slice(2);
const step = (s) => console.log(`STEP ${s}`);
const stdin = readline.createInterface({ input: process.stdin });
const lines = stdin[Symbol.asyncIterator]();

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
await page.goto(url);
await page.waitForFunction(() => document.querySelector("#status").textContent.includes("visible as"));
if (page.url().includes("#t=")) throw new Error("token stays in the address bar");
step("status " + (await page.textContent("#status")));

await page.click("#refresh");
await page.waitForSelector("#devices .card .name:text('phone')", { timeout: 15000 });
step("device found");

await page.evaluate(() => {
  const card = [...document.querySelectorAll("#devices .card")].find((c) => c.textContent.includes("phone"));
  const dt = new DataTransfer();
  dt.items.add(new File(["hello from the browser"], "hello.txt", { type: "text/plain" }));
  card.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
});
await page.waitForSelector("#jobs .job.done", { timeout: 15000 });
step("upload sent: " + (await page.textContent("#jobs .job.done")));
await page.screenshot({ path: `${shots}/ui-sent.png`, fullPage: true });

console.log("READY_FOR_OFFER");
await page.waitForSelector("#offers .offer button.primary", { timeout: 15000 });
const offerText = await page.textContent("#offers .offer");
step("offer shown: " + offerText.replace(/\s+/g, " "));
await page.screenshot({ path: `${shots}/ui-offer.png`, fullPage: true });
await page.click("#offers .offer button.primary");
await lines.next();   // python: "RECEIVED"
await page.waitForFunction(() => document.querySelector("#log").textContent.includes("received"));
step("log: " + (await page.textContent("#log li")));
// Browser download: share a file, then act as the phone's browser.
await page.evaluate(() => {
  const dt = new DataTransfer();
  dt.items.add(new File(["for the iphone"], "for-the-phone.txt", { type: "text/plain" }));
  document.querySelector("#share-card").dispatchEvent(
    new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
});
await page.waitForSelector("#share:not([hidden]) #share-qr svg", { timeout: 10000 });
const shareUrl = await page.textContent("#share-url");
const pin = await page.textContent("#share-pin");
step(`share url ${shareUrl.replace(/k=.*/, "k=…")} pin ${pin.length} digits`);
await page.screenshot({ path: `${shots}/ui-share.png`, fullPage: true });
const phone = await browser.newPage();
await phone.goto(shareUrl);
await phone.fill("input[name=pin]", pin);
await phone.click("button");
await phone.waitForSelector("li a");
const href = await phone.getAttribute("li a", "href");
const got = await (await phone.request.get(new URL(href, shareUrl).toString())).text();
step(`phone downloaded: ${got}`);
await phone.screenshot({ path: `${shots}/phone-share.png` });
await page.waitForFunction(() => document.querySelector("#share-info").textContent.includes("1 Download"));
await page.click("#share-stop");
await page.waitForSelector("#share[hidden]", { state: "attached" });
step("share stopped");

// Accept only some files of an offer (LocalSend lets the receiver choose).
console.log("READY_FOR_MULTI");
await page.waitForSelector("#offers .offer [data-action=accept-selected]", { timeout: 15000 });
await page.uncheck("#offers .offer label:has-text('zwei.jpg') input");
await page.screenshot({ path: `${shots}/ui-offer-select.png`, fullPage: true });
await page.click("#offers .offer [data-action=accept-selected]");
await lines.next();   // python: "MULTI_DONE"
await page.waitForFunction(() => document.querySelector("#log").textContent.includes("(2 von 3)"));
step("partial accept logged: " + (await page.textContent("#log li:has-text('2 von 3')")));

// LocalSend receiver with a PIN: first try fails, the job offers a PIN field.
console.log("READY_FOR_PIN");
const [, tabletPort, tabletPin] = (await lines.next()).value.split(" ");
await page.click("details summary");
await page.fill("#manual-address", `127.0.0.1:${tabletPort}`);
await page.selectOption("#manual-protocol", "localsend");
await page.click("#manual button[type=submit]");
await page.evaluate((port) => {
  const card = [...document.querySelectorAll("#devices .card")].find((c) => c.textContent.includes(`:${port}`));
  const dt = new DataTransfer();
  dt.items.add(new File(["geheim"], "fuer-tablet.txt", { type: "text/plain" }));
  card.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
}, tabletPort);
await page.waitForSelector("#jobs .job.failed .pin-form input", { timeout: 15000 });
step("PIN asked: " + (await page.textContent("#jobs .job.failed")).replace(/\s+/g, " "));
await page.fill("#jobs .job.failed .pin-form input", tabletPin);
await page.screenshot({ path: `${shots}/ui-pin.png`, fullPage: true });
await page.click("#jobs .job.failed .pin-form button");
await page.waitForSelector("#jobs .job.done:has-text('fuer-tablet.txt')", { timeout: 15000 });
step("sent with PIN: " + (await page.textContent("#jobs .job.done:has-text('fuer-tablet.txt')")));

// History survives a daemon restart.
console.log("READY_FOR_RESTART");
const [, newUrl] = (await lines.next()).value.split(" ");
await page.goto(newUrl);
await page.waitForFunction(() => document.querySelector("#status").textContent.includes("visible as"));
await page.waitForSelector("#log li.old", { timeout: 10000 });
const old = await page.$$eval("#log li.old", (li) => li.map((l) => l.textContent));
if (!old.some((t) => t.includes("empfangen: vom-handy.jpg"))) throw new Error("history lost: " + old);
step(`after restart: ${old.length} older entries, e.g. "${old.find((t) => t.includes("received"))}"`);
await page.screenshot({ path: `${shots}/ui-history.png`, fullPage: true });

if (errors.length) { console.log("JS ERRORS " + JSON.stringify(errors)); process.exitCode = 1; }
await browser.close();
console.log("DONE");
process.exit(process.exitCode || 0);
