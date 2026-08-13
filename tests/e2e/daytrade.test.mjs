// 當沖候選頁：三個榜都要出得來，且「不是進出訊號」的警語與上市/上櫃門檻差異
// 必須顯示——代理指標不標示清楚就會被當成同一個標準。
//
// daytrade.json 由管線產出，不提交 fixture（本機驗證用的那份是模擬當沖比率，
// 提交進 repo 會把虛構數字混進版控）。所以資料未就緒時測空狀態路徑，
// 有資料時才跑完整斷言——兩條路徑都要正確。
import { startServer, launchBrowser, assert } from "./serve.mjs";

const { server, base } = await startServer();
const browser = await launchBrowser();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.goto(`${base}/index.html`, { waitUntil: "networkidle" });
await page.click('button[data-tab="daytrade"]');
await page.waitForTimeout(1800);

const hasData = await page.evaluate(async () => {
  try {
    const payload = await (await fetch("data/daytrade.json")).json();
    return payload.state === "ready";
  } catch { return false; }
});

const v = await page.evaluate(() => ({
  visible: document.querySelector("#daytrade-view")?.style.display !== "none",
  title: document.querySelector("#daytrade-title")?.textContent || "",
  note: document.querySelector("#daytrade-note")?.textContent || "",
  warn: document.querySelector("#daytrade-view .empty")?.textContent || "",
  gates: document.querySelector("#daytrade-gates")?.textContent || "",
  mom: document.querySelectorAll("#daytrade-momentum tbody tr").length,
  rev: document.querySelectorAll("#daytrade-reversal tbody tr").length,
  pre: document.querySelectorAll("#daytrade-pressure tbody tr").length,
  pills: document.querySelectorAll("#daytrade-view .status-pill.warn").length,
}));

// 這兩項與資料無關，任何情況都必須成立
assert(v.visible, "當沖 view 應顯示");
assert(/盤前候選名單，不是進出訊號/.test(v.warn),
  "必須顯示『不是進出訊號』的警語——這是這個頁面最重要的一句話");

if (hasData) {
  assert(/上市門檻/.test(v.gates) && /代理指標/.test(v.gates),
    "必須並列上市/上櫃門檻並標示上櫃是代理指標");
  assert(v.mom > 0 && v.rev > 0, "追強與接刀榜應有資料");
  assert(v.pills > 0, "應有代理門檻或隔日沖的警示標記");
  console.log(`OK 當沖候選（有資料）：追強 ${v.mom} / 接刀 ${v.rev} / 賣壓 ${v.pre}`);
} else {
  assert(/尚無候選|讀取失敗/.test(v.note),
    `資料未就緒時應顯示說明而非留白，實得「${v.note}」`);
  console.log("OK 當沖候選（資料未就緒，已驗證空狀態與警語）");
}

assert(errors.length === 0, `不應有 JS 例外：${errors.join(" | ")}`);

await browser.close();
server.close();
