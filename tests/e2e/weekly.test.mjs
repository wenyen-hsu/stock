// 週報分頁：三個區塊都要渲染，行事曆預設只顯示重點事件（財報董事會佔 73%，
// 全部平鋪會出現單日 300+ 個 chip 把法說會淹掉），展開鈕要能切回全部。
import { startServer, launchBrowser, assert } from "./serve.mjs";

const { server, base } = await startServer();
const browser = await launchBrowser();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", error => errors.push(error.message));

await page.goto(`${base}/index.html`, { waitUntil: "networkidle" });
await page.click('button[data-tab="weekly"]');
await page.waitForTimeout(1500);

const view = await page.evaluate(() => ({
  visible: document.querySelector("#weekly-view")?.style.display !== "none",
  title: document.querySelector("#weekly-title")?.textContent || "",
  note: document.querySelector("#weekly-note")?.textContent || "",
  market: document.querySelector("#weekly-market")?.textContent || "",
  mainstreamRows: document.querySelectorAll("#weekly-mainstream tbody tr").length,
  speculativeRows: document.querySelectorAll("#weekly-speculative tbody tr").length,
  sectorInRows: document.querySelectorAll("#weekly-sector-in tbody tr").length,
  foreignRows: document.querySelectorAll("#weekly-foreign tbody tr").length,
  chips: document.querySelectorAll("#weekly-upcoming .digest-chip").length,
  toggle: document.querySelector("#weekly-events-toggle")?.textContent.trim() || "",
}));

assert(view.visible, "週報 view 應顯示");
assert(/\d{4}-\d{2}-\d{2}\s*～\s*\d{4}-\d{2}-\d{2}/.test(view.title), `標題應含週期區間，實得「${view.title}」`);
assert(view.mainstreamRows > 0, "主流強勢股應有資料");
assert(view.sectorInRows > 0, "資金流入族群應有資料");
assert(view.foreignRows > 0, "外資週買超應有資料");
assert(/大盤週漲跌/.test(view.market), "大盤指標卡應渲染");

// 分層：預設顯示的 chip 數量必須遠少於總事件數
const totalEvents = await page.evaluate(async () => (await (await fetch("data/weekly.json")).json()).upcoming.length);
assert(totalEvents > 0, "測試資料應含下週事件");
assert(view.chips < totalEvents, `預設應摺疊低訊號事件（總 ${totalEvents} 件，顯示 ${view.chips} 個）`);
assert(/展開其餘/.test(view.toggle), `應有展開鈕，實得「${view.toggle}」`);

await page.click("#weekly-events-toggle");
await page.waitForTimeout(300);
const expanded = await page.evaluate(() => ({
  chips: document.querySelectorAll("#weekly-upcoming .digest-chip").length,
  toggle: document.querySelector("#weekly-events-toggle")?.textContent.trim() || "",
}));
assert(expanded.chips === totalEvents, `展開後應顯示全部 ${totalEvents} 件，實得 ${expanded.chips}`);
assert(/只看重點/.test(expanded.toggle), "展開後鈕應可切回");

// 點事件 chip 要能進個股頁（openDetail 整合）
await page.click("#weekly-upcoming .digest-chip");
await page.waitForTimeout(1500);
const afterClick = await page.evaluate(() => ({
  detailVisible: document.querySelector("#detail-view")?.style.display !== "none",
  backLabel: document.querySelector("#nav-back")?.textContent || "",
}));
assert(afterClick.detailVisible, "點事件應切到個股頁");

assert(errors.length === 0, `不應有未捕捉的 JS 例外：${errors.join(" | ")}`);
console.log(`OK 週報：主流 ${view.mainstreamRows} 檔、投機 ${view.speculativeRows} 檔、`
  + `族群流入 ${view.sectorInRows}、事件 ${view.chips}/${totalEvents} 件`);

await browser.close();
server.close();
