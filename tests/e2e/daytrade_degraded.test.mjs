// 當沖統計缺漏時的降級必須「看得見」。
//
// 2026-08-13/14 的事故裡，整個上市市場從榜單消失，而畫面上沒有任何異樣——
// 標題、門檻說明、三個榜都正常渲染，只是 54 檔候選全是上櫃。這個測試把那個
// 狀態餵給頁面，確認它現在會自己講出來。
//
// payload 是合成的，所以寫到暫存目錄而非 docs/：真實 daytrade.json 由管線產出，
// 把捏造的數字提交進 repo 會混進版控。
import { mkdtemp, writeFile, cp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startServer, launchBrowser, assert } from "./serve.mjs";

// 整包複製：頁面開機就會讀 meta.json 等檔案，只放 daytrade.json 會卡在載入中。
const docsDir = process.env.DOCS_DIR || "docs";
const root = await mkdtemp(join(tmpdir(), "daytrade-degraded-"));
await cp(docsDir, root, { recursive: true });

const candidate = (stock_id, market, gate_basis) => ({
  stock_id, name: `股${stock_id}`, market, gate_basis,
  close: 100, industry: "半導體業", sub_industry: "",
  day_trade_pct: null, avg_turnover_100m: 8, chg_1d_pct: 3.2,
  amplitude_pct: 5.1, volume_ratio_1d: 2.1, foreign_net_lot: 0,
  trust_net_lot: 0, is_day_trader_branch: false, top_buy_branch_name: "",
  ceiling_queue_lot: null, in_strong_sector: false,
  margin_short_balance_lot: 0, score: 10,
});

const pool = [
  candidate("2330", "TWSE", "turnover_proxy_fallback"),
  candidate("6488", "TPEX", "turnover_proxy"),
];
await writeFile(join(root, "data", "daytrade.json"), JSON.stringify({
  state: "ready",
  trade_date: "2026-08-14",
  candidate_count: pool.length,
  strong_sector_count: 3,
  day_trade_stat_count: 0,
  day_trade_stats_available: false,
  gates: {
    twse: { turnover_100m: 5.0, note: "當沖統計缺漏，上市本日改用日均額代理" },
    tpex: { turnover_100m: 5.0, note: "上櫃無當沖統計，以日均額代理" },
    close_range: [10.0, 500.0],
  },
  rankings: { momentum: pool, reversal: pool, pressure: [] },
  excluded_disposal: [],
}));

const { server, base } = await startServer(root);
const browser = await launchBrowser();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.goto(`${base}/index.html`, { waitUntil: "networkidle" });
await page.click('button[data-tab="daytrade"]');
await page.waitForTimeout(1500);

const v = await page.evaluate(() => ({
  note: document.querySelector("#daytrade-note")?.textContent || "",
  gates: document.querySelector("#daytrade-gates")?.textContent || "",
  mom: document.querySelectorAll("#daytrade-momentum tbody tr").length,
  body: document.querySelector("#daytrade-momentum")?.textContent || "",
}));

assert(/當沖統計缺漏/.test(v.note),
  `說明列必須點出統計缺漏，實得「${v.note}」`);
assert(/降級為代理/.test(v.gates),
  `上市門檻區塊必須標示已降級，實得「${v.gates}」`);
assert(!/當沖比率\s*≥/.test(v.gates),
  "降級時不可再宣稱套用了當沖比率門檻——那是這次事故最誤導的一句話");
assert(v.mom === 2, `上市不可消失，應有 2 檔，實得 ${v.mom}`);
assert(/統計缺漏/.test(v.body), "個股列要帶降級標記");
assert(errors.length === 0, `不應有 JS 例外：${errors.join(" | ")}`);

console.log(`OK 當沖降級：說明列、門檻標示與 ${v.mom} 檔候選都正確`);

await browser.close();
server.close();
