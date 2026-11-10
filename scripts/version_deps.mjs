// 영상 페이지를 처음부터 끝까지 넘기며 실제로 읽는 파일을 기록한다(version.py release 가 부른다).
// 사용법: node version_deps.mjs <프로젝트 폴더> <결과 json> [--page reel/index.html] [--step 0.5]
// 결과: { "page": "reel/index.html", "duration": 48, "files": ["reel/index.html", "reel/engine.js", "assets/img/a.png", …] }
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const args = process.argv.slice(2);
const opt = { page: 'reel/index.html', step: '0.5' };
const pos = [];
for (let i = 0; i < args.length; i++) {
  if (args[i].startsWith('--')) { opt[args[i].slice(2)] = args[i + 1]; i++; } else pos.push(args[i]);
}
const root = path.resolve(pos[0]);
const outFile = pos[1];
const seen = new Set();
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '');
  const file = path.join(root, rel);
  if (!file.startsWith(root)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end(); }
    seen.add(rel.split(path.sep).join('/'));
    const ext = path.extname(file).toLowerCase();
    const type = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
      '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
      '.ttf': 'font/ttf', '.otf': 'font/otf', '.woff': 'font/woff', '.woff2': 'font/woff2' }[ext] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(data);
  });
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const exe = process.env.REEL_CHROME_PATH;
const tries = exe ? [{ executablePath: exe }] : [{ channel: process.env.REEL_CHROME_CHANNEL || 'chrome' }, { channel: 'chromium' }, {}];
let browser, last;
for (const t of tries) { try { browser = await chromium.launch({ headless: true, ...t }); break; } catch (e) { last = e; } }
if (!browser) { console.error('브라우저를 띄우지 못했다\n' + last); process.exit(1); }
const page = await browser.newPage();
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.goto(`http://127.0.0.1:${server.address().port}/${opt.page}?render=1`, { waitUntil: 'load' });
await page.waitForFunction(() => window.__ready === true, null, { timeout: 120000 });
const duration = await page.evaluate(() => window.__meta.duration);
const step = Number(opt.step);
for (let t = 0; t <= duration; t += step) await page.evaluate(at => window.__seek(at), t);
await page.waitForTimeout(300);
await browser.close();
server.close();
fs.writeFileSync(outFile, JSON.stringify({ page: opt.page, duration, files: [...seen].sort(), errors }, null, 1));
console.log(`읽은 파일 ${seen.size}개 (${duration}s)`);
