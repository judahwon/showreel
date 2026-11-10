// 컷 페이지를 처음부터 끝까지 넘기며 실제로 읽는 assets 파일을 기록한다(reel.py deps 가 부른다). 사용법: node record_deps.mjs <project 폴더> <결과 json> [컷…] (컷을 적으면 그 컷만)
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const work = path.resolve(process.argv[2]);
const spec = JSON.parse(fs.readFileSync(path.join(work, 'cuts.json'), 'utf8'));
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.ttf': 'font/ttf', '.woff2': 'font/woff2' };
const server = http.createServer((req, res) => {
  const file = path.join(work, decodeURIComponent(new URL(req.url, 'http://x').pathname));
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(data);
  });
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const executablePath = process.env.REEL_CHROME_PATH;
const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : { channel: process.env.REEL_CHROME_CHANNEL || 'chrome' }) });
const only = process.argv.slice(4);
const result = {};
for (const cut of spec.cuts.filter((item) => !only.length || only.includes(item.id))) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const seen = new Set();
  page.on('request', (request) => {
    const pathname = decodeURIComponent(new URL(request.url()).pathname);
    if (pathname.startsWith('/assets/')) seen.add(pathname.slice(1));
  });
  await page.goto(`http://127.0.0.1:${ server.address().port }/reel/${ cut.page }?render=1`);
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 120000 });
  const duration = await page.evaluate(() => window.__meta.duration);
  for (let t = 0; t < duration; t += 0.5) await page.evaluate((at) => window.__seek(at), t);
  await page.waitForTimeout(300);
  result[cut.id] = [...seen].sort();
  console.log(cut.id, seen.size);
  await page.close();
}
await browser.close();
server.close();
fs.writeFileSync(process.argv[3], JSON.stringify(result, null, 1));
