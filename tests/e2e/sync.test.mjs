// 雲端同步端到端（模擬 GitHub Contents API）：推送、跨裝置拉取、刪除、
// 墓碑不復活、409 衝突重試、中文備註 round-trip。
import { startServer, launchBrowser, assert } from "./serve.mjs";

let store = null;
let shaCounter = 0;
function unb64(text) { return Buffer.from(text, "base64").toString("utf8"); }

const { server, base } = await startServer();
const browser = await launchBrowser();

async function newDevice() {
  const ctx = await browser.newContext();
  await ctx.route("https://api.github.com/**", async route => {
    const req = route.request();
    if (req.method() === "GET") {
      if (!store) return route.fulfill({ status: 404, contentType: "application/json", body: '{"message":"Not Found"}' });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ content: store.content, sha: store.sha }) });
    }
    if (req.method() === "PUT") {
      const body = req.postDataJSON();
      if (store && body.sha !== store.sha) return route.fulfill({ status: 409, contentType: "application/json", body: '{"message":"sha mismatch"}' });
      store = { content: body.content, sha: `sha${++shaCounter}` };
      return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ content: { sha: store.sha } }) });
    }
    route.continue();
  });
  const page = await ctx.newPage();
  page.on("pageerror", err => { console.error("PAGE ERROR:", err.message); process.exit(1); });
  await page.goto(`${base}/index.html`, { waitUntil: "networkidle" });
  await page.evaluate(() => {
    localStorage.setItem("stockChipSyncRepo", "user/portfolio");
    localStorage.setItem("stockChipSyncToken", "test-token");
  });
  return page;
}
const remoteIds = () => store ? JSON.parse(unb64(store.content)).trades.map(t => t.id) : null;
async function openPortfolio(page) {
  await page.evaluate(() => document.querySelector('button[data-tab="ranking"]').click());
  await page.waitForTimeout(150);
  await page.evaluate(() => document.querySelector('button[data-tab="portfolio"]').click());
  await page.waitForTimeout(900);
}

const A = await newDevice();
await A.evaluate(() => {
  localStorage.setItem("stockChipTrades", JSON.stringify([
    { id: 1, date: "2026-06-10", stock_id: "2330", side: "buy", shares: 1000, price: 1000, fee: 1425, tax: 0, note: "測試中文備註🔥" },
    { id: 2, date: "2026-06-25", stock_id: "2890", side: "buy", shares: 2000, price: 38.5, fee: 110, tax: 0, note: "" },
  ]));
});
await openPortfolio(A);
assert(JSON.stringify(remoteIds()) === "[1,2]", `裝置A推送後雲端 [1,2]（實際 ${JSON.stringify(remoteIds())}）`);

const B = await newDevice();
await openPortfolio(B);
const bIds = await B.evaluate(() => JSON.parse(localStorage.getItem("stockChipTrades")).map(t => t.id));
assert(JSON.stringify(bIds) === "[1,2]", `裝置B自動拉取 [1,2]（實際 ${JSON.stringify(bIds)}）`);
const noteBack = await B.evaluate(() => JSON.parse(localStorage.getItem("stockChipTrades"))[0].note);
assert(noteBack === "測試中文備註🔥", "中文備註 round-trip");

B.on("dialog", dialog => dialog.accept());
await B.evaluate(() => { [...document.querySelectorAll(".pf-del")].find(btn => btn.closest("tr").textContent.includes("2890"))?.click(); });
await B.waitForTimeout(900);
assert(JSON.stringify(remoteIds()) === "[1]", `裝置B刪除後雲端 [1]（實際 ${JSON.stringify(remoteIds())}）`);

await openPortfolio(A);
const aIds = await A.evaluate(() => JSON.parse(localStorage.getItem("stockChipTrades")).map(t => t.id));
assert(!aIds.includes(2), `裝置A重開後刪除不復活（實際 ${JSON.stringify(aIds)}）`);

// 409 衝突：A 端持 stale sha 推送 → 自動重拉重推
store = { ...store, sha: `sha${++shaCounter}` };  // 模擬他端搶先推送改變 sha
await A.evaluate(() => {
  const trades = JSON.parse(localStorage.getItem("stockChipTrades"));
  trades.push({ id: 9, date: "2026-07-02", stock_id: "2317", side: "buy", shares: 1000, price: 200, fee: 285, tax: 0, note: "" });
  localStorage.setItem("stockChipTrades", JSON.stringify(trades));
});
await openPortfolio(A);
assert((remoteIds() || []).includes(9), `409 重試後新交易上雲（實際 ${JSON.stringify(remoteIds())}）`);

await browser.close();
server.close();
console.log("SYNC E2E PASSED");
