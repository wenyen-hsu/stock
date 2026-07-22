// E2E 共用：靜態站 http server + headless Chromium 啟動。
// DOCS_DIR 指定站台目錄（預設 repo 的 docs/，CI 會先從 site 分支放入 data/）；
// Chromium 來源：playwright 套件內建優先，否則用 PW_CHROMIUM_PATH（沙箱預裝路徑）。
import http from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
};

export async function startServer(docsDir = process.env.DOCS_DIR || "docs") {
  const server = http.createServer(async (req, res) => {
    try {
      const path = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname)).replace(/^([.][.][/\\])+/, "");
      const file = join(docsDir, path === "/" || path === "\\" ? "index.html" : path);
      const body = await readFile(file);
      res.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream" });
      res.end(body);
    } catch {
      res.writeHead(404);
      res.end("not found");
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { server, base: `http://127.0.0.1:${port}` };
}

export async function launchBrowser() {
  try {
    const { chromium } = await import("playwright");
    return chromium.launch({ args: ["--no-sandbox"] });
  } catch {
    const { chromium } = await import("playwright-core");
    const executablePath = process.env.PW_CHROMIUM_PATH;
    if (!executablePath) throw new Error("需安裝 playwright 或設 PW_CHROMIUM_PATH");
    return chromium.launch({ executablePath, args: ["--no-sandbox"] });
  }
}

export function assert(condition, message) {
  if (!condition) {
    console.error(`❌ ${message}`);
    process.exit(1);
  }
  console.log(`✓ ${message}`);
}
