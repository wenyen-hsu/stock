// 同步競態端到端：推送飛行中新增不遺失、飛行中刪除不復活、樂觀渲染不被網路阻塞。
import { startServer, launchBrowser, assert } from "./serve.mjs";

let store = null;
let shaCounter = 0;
let putDelayMs = 0;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function unb64(text) { return Buffer.from(text, "base64").toString("utf8"); }

const { server, base } = await startServer();
const browser = await launchBrowser();
const ctx = await browser.newContext();
await ctx.route("https://api.github.com/**", async route => {
  const req = route.request();
  if (req.method() === "GET") {
    if (!store) return route.fulfill({ status: 404, contentType: "application/json", body: '{"message":"Not Found"}' });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ content: store.content, sha: store.sha }) });
  }
  if (req.method() === "PUT") {
    if (putDelayMs) await sleep(putDelayMs);
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
  localStorage.setItem("stockChipTrades", JSON.stringify([
    { id: 1, date: "2026-06-10", stock_id: "2330", side: "buy", shares: 1000, price: 1000, fee: 1425, tax: 0, note: "" },
  ]));
});
const localIds = () => page.evaluate(() => JSON.parse(localStorage.getItem("stockChipTrades")).map(t => t.id));
const remoteIds = () => store ? JSON.parse(unb64(store.content)).trades.map(t => t.id) : null;
async function openPortfolio() {
  await page.evaluate(() => document.querySelector('button[data-tab="ranking"]').click());
  await page.waitForTimeout(150);
  await page.evaluate(() => document.querySelector('button[data-tab="portfolio"]').click());
}

await openPortfolio(); await page.waitForTimeout(1000);

// 競態 1：PUT 飛行中新增交易 → 不得遺失
putDelayMs = 1500;
store = null;  // 清空遠端使本輪必推
await openPortfolio();
await page.waitForTimeout(400);
await page.evaluate(() => {
  const trades = JSON.parse(localStorage.getItem("stockChipTrades"));
  trades.push({ id: 3, date: "2026-07-02", stock_id: "3017", side: "buy", shares: 500, price: 300, fee: 214, tax: 0, note: "" });
  localStorage.setItem("stockChipTrades", JSON.stringify(trades));
});
await page.waitForTimeout(2200);
putDelayMs = 0;
await openPortfolio(); await page.waitForTimeout(1000);
assert((await localIds()).includes(3) && (remoteIds() || []).includes(3), "PUT 飛行中新增的交易存活並補推");

// 競態 2：PUT 飛行中刪除 → 不得復活
putDelayMs = 1500;
{
  const payload = JSON.parse(unb64(store.content));
  payload.trades.push({ id: 4, date: "2026-07-03", stock_id: "2454", side: "buy", shares: 100, price: 1500, fee: 214, tax: 0, note: "" });
  store = { content: Buffer.from(JSON.stringify(payload)).toString("base64"), sha: `sha${++shaCounter}` };
}
await openPortfolio();
await page.waitForTimeout(400);
await page.evaluate(() => {
  localStorage.setItem("stockChipTrades", JSON.stringify(JSON.parse(localStorage.getItem("stockChipTrades")).filter(t => t.id !== 1)));
  const tomb = JSON.parse(localStorage.getItem("stockChipTradeTombstones") || "[]");
  tomb.push(1);
  localStorage.setItem("stockChipTradeTombstones", JSON.stringify(tomb));
});
await page.waitForTimeout(2200);
putDelayMs = 0;
await openPortfolio(); await page.waitForTimeout(1000);
assert(!(await localIds()).includes(1) && !(remoteIds() || []).includes(1), "PUT 飛行中刪除的交易不復活");

// 樂觀渲染：PUT 延遲 3 秒下開頁應立即有畫面
putDelayMs = 3000;
const t0 = Date.now();
await openPortfolio();
await page.waitForFunction(() => document.querySelector("#pf-overview").children.length > 0);
const renderMs = Date.now() - t0;
assert(renderMs < 1500, `樂觀渲染 ${renderMs}ms < 1500ms（PUT 延遲 3000ms）`);

await browser.close();
server.close();
console.log("RACE E2E PASSED");
