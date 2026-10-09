// Drive the running app like a user and capture frames along the way.
// Usage: node scripts/drive.mjs <outdir> <scenario> [--light]
import { chromium } from "../web/node_modules/playwright-core/index.mjs";

const [outdir, scenario] = process.argv.slice(2);
const scheme = process.argv.includes("--light") ? "light" : "dark";
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, colorScheme: scheme, deviceScaleFactor: 2 });
const logs = [];
page.on("console", (m) => m.type() !== "debug" && logs.push(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => logs.push(`[pageerror] ${e.message}`));
const shot = (name) => page.screenshot({ path: `${outdir}/${name}-${scheme}.png` });

await page.goto(process.env.APP_URL || "http://127.0.0.1:8765/");
await page.waitForTimeout(1200);

if (scenario === "profile") {
  await page.fill("#name", "Vinayak");
  await page.selectOption("#tz", "Asia/Kolkata").catch(() => page.selectOption("#tz", "Asia/Calcutta"));
  await page.click("button:has-text('Continue')");
  await page.waitForSelector(".composer textarea", { timeout: 15000 });
  await page.waitForTimeout(1200);
  await shot("chat-empty");
} else if (scenario.startsWith("ask:")) {
  const question = scenario.slice(4);
  await page.click(".icon-button[aria-label='Conversations']");
  await page.waitForTimeout(500);
  await page.click(".drawer-new");
  await page.waitForTimeout(600);
  await shot("chat-empty");
  await page.fill(".composer textarea", question);
  await page.waitForTimeout(400);
  await shot("typing");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1500);
  await shot("thinking-1");
  await page.waitForSelector(".thinking-trail", { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(800);
  await shot("thinking-2");
  await page.waitForSelector(".answer", { timeout: 90000 });
  await page.waitForTimeout(700);
  await shot("streaming");
  await page.waitForSelector(".composer .send[aria-label='Send']", { timeout: 120000 });
  await page.waitForTimeout(1200);
  await shot("answered");
  const cite = await page.$(".cite-chip");
  if (cite) {
    await cite.hover();
    await page.waitForTimeout(400);
    await shot("citation-hover");
  }
}
if (scenario.startsWith("approval:")) {
  // Ends by clicking Discard: nothing is ever sent by this scenario.
  await page.click(".icon-button[aria-label='Conversations']");
  await page.waitForTimeout(500);
  await shot("drawer");
  await page.click(".drawer-new");
  await page.waitForTimeout(500);
  await page.fill(".composer textarea", scenario.slice(9));
  await page.keyboard.press("Enter");
  await page.waitForSelector(".approval", { timeout: 120000 });
  await page.waitForTimeout(1500);
  await shot("approval");
  await page.click(".approval .button.quiet");
  await page.waitForTimeout(1200);
  await shot("discarded");
}
if (scenario === "mobile") {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(800);
  await shot("mobile");
}
await browser.close();
console.log(logs.join("\n") || "no console errors");
