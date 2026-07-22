// 記帳引擎端到端：平均成本、費稅、已實現損益（只斷言成本端，不依賴當日行情）。
import { startServer, launchBrowser, assert } from "./serve.mjs";

const { server, base } = await startServer();
const browser = await launchBrowser();
const page = await (await browser.newContext()).newPage();
page.on("pageerror", err => { console.error("PAGE ERROR:", err.message); process.exit(1); });
await page.goto(`${base}/index.html`, { waitUntil: "networkidle" });

await page.evaluate(() => {
  localStorage.setItem("stockChipTrades", JSON.stringify([
    { id: 1, date: "2026-06-10", stock_id: "2330", side: "buy", shares: 1000, price: 1000, fee: 1425, tax: 0, note: "" },
    { id: 2, date: "2026-06-20", stock_id: "2330", side: "buy", shares: 1000, price: 1100, fee: 1568, tax: 0, note: "" },
    { id: 3, date: "2026-07-01", stock_id: "2330", side: "sell", shares: 500, price: 1200, fee: 855, tax: 1800, note: "" },
  ]));
});
const result = await page.evaluate(() => {
  const { holdings, realizedTotal } = replayPortfolio(JSON.parse(localStorage.getItem("stockChipTrades")));
  const slot = holdings["2330"];
  return { shares: slot.shares, avg: Math.round(slot.cost / slot.shares * 100) / 100, realized: Math.round(realizedTotal) };
});
assert(result.shares === 1500, `賣出後持股 1500（實際 ${result.shares}）`);
assert(result.avg === 1051.5, `平均成本 1051.5（實際 ${result.avg}）`);
assert(result.realized === 71597, `已實現 71,597（實際 ${result.realized}）`);

// 除權息重放：跨越除息/除權日
const div = await page.evaluate(() => {
  const trades = [
    { id: 1, date: "2026-06-01", stock_id: "2330", side: "buy", shares: 1000, price: 1000, fee: 1425, tax: 0 },
    { id: 2, date: "2026-06-20", stock_id: "2890", side: "buy", shares: 2000, price: 38.5, fee: 110, tax: 0 },
  ];
  const map = {
    "2330": [{ ex_date: "2026-06-12", cash: 5, stock: 0 }, { ex_date: "2099-01-01", cash: 5, stock: 0 }],
    "2890": [{ ex_date: "2026-07-01", cash: 0.6, stock: 0.1 }],
  };
  const { holdings, dividendIncome } = replayPortfolio(trades, map);
  return { income: Math.round(dividendIncome), shares2890: holdings["2890"].shares, avg2890: Math.round(holdings["2890"].cost / holdings["2890"].shares * 100) / 100 };
});
assert(div.income === 6200, `股利入帳 6,200（實際 ${div.income}）`);
assert(div.shares2890 === 2200, `配股後 2,200 股（實際 ${div.shares2890}）`);
assert(div.avg2890 === 35.05, `攤薄均價 35.05（實際 ${div.avg2890}）`);

await browser.close();
server.close();
console.log("PORTFOLIO E2E PASSED");
