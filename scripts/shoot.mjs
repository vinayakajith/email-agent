// Real-time screenshots of the running app with the installed Chrome (no browser download).
// Usage: node scripts/shoot.mjs <out.png> [--dark|--light] [--wait ms] [--js "code to run first"]
import { chromium } from "../web/node_modules/playwright-core/index.mjs";

const args = process.argv.slice(2);
const out = args[0];
const scheme = args.includes("--light") ? "light" : "dark";
const wait = Number(args[args.indexOf("--wait") + 1]) || 2500;
const js = args.includes("--js") ? args[args.indexOf("--js") + 1] : null;
const url = process.env.APP_URL || "http://127.0.0.1:8765/";

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, colorScheme: scheme, deviceScaleFactor: 2 });
const logs = [];
page.on("console", (m) => logs.push(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => logs.push(`[pageerror] ${e.message}`));
await page.goto(url);
await page.waitForTimeout(wait);
if (js) {
  await page.evaluate(js);
  await page.waitForTimeout(wait);
}
await page.screenshot({ path: out });
await browser.close();
if (logs.length) console.log(logs.join("\n"));
