#!/usr/bin/env node
// showreel 렌더러 — 결정적 HTML 타임라인(engine.js)을 프레임 단위로 캡처해 MP4 로 만든다.
//
// 사용법
//   영상:  node render.mjs --dir <project>/reel --out out/video.mp4 [--scale 1] [--workers 4] [--sub 1]
//                         [--format png|jpeg] [--crf 16] [--from 0] [--to 60] [--fps 30] [--chunk 2]
//   스틸:  node render.mjs --dir <project>/reel --stills 0.5,3,12.25 [--every 1] --outdir qa/stills [--scale 0.5]
//   미리보기 서버: node render.mjs --dir <project>/reel --serve      (URL 을 출력하고 계속 띄워 둔다)
//
// 옵션
//   --scale 0.5     초안(960×540). 최종은 1
//   --sub 4         서브프레임 모션 블러(셔터 180°). 최종 렌더에서만. 4 → 렌더 시간 약 4배
//   --format        jpeg(기본, 빠름) | png(무손실, 느림). 둘 다 BT.709 tv 범위로 정확히 변환한다
//   --chunk 2       청크 길이(초). 청크마다 세그먼트 파일을 만들어, 중단돼도 다시 실행하면 이어서 렌더한다(--fresh 로 무시)
//   --workers N     동시 브라우저 페이지 수. 기본 = CPU 코어/3 (최대 6). 다른 렌더가 돌고 있으면 줄인다
//
// 페이지 규약: window.__ready === true 가 되면 준비 완료, window.__seek(t) 로 t초 화면, window.__meta = {duration,fps,width,height,fonts}
// 종료 코드: 0 정상, 1 사용법·실행 오류, 2 페이지 오류(pageerror) 발생 — 결과물을 반드시 확인할 것
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright-core')); }
catch { console.error('playwright-core 가 없다. 한 번만: cd ' + path.dirname(fileURLToPath(import.meta.url)) + ' && npm install'); process.exit(1); }

const argv = process.argv.slice(2);
const opt = {};
for (let i = 0; i < argv.length; i++) {
  const k = argv[i];
  if (!k.startsWith('--')) continue;
  const v = argv[i + 1] !== undefined && !argv[i + 1].startsWith('--') ? argv[++i] : 'true';
  opt[k.slice(2)] = v;
}
if (!opt.dir) { console.error('사용법: node render.mjs --dir <reel 폴더> (--out out.mp4 | --stills t1,t2 | --serve)'); process.exit(1); }
const dir = path.resolve(opt.dir);
const root = path.resolve(opt.root || path.dirname(dir));     // 서버 루트: reel 의 부모(프로젝트) — ../assets, ../audio 접근용
const pagePath = '/' + path.relative(root, path.join(dir, opt.page || 'index.html')).split(path.sep).join('/');
const scale = Number(opt.scale || 1);
const format = opt.format === 'png' ? 'png' : 'jpeg';
const quality = Number(opt.quality || 95);
const crf = String(opt.crf || 16);
const preset = opt.preset || 'medium';
const sub = Math.max(1, Number(opt.sub || 1));
const shutter = Number(opt.shutter || 0.5);
const cpu = os.cpus().length;
const workers = Math.max(1, Number(opt.workers || Math.min(6, Math.max(1, Math.floor(cpu / 3)))));

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.ttf': 'font/ttf',
  '.otf': 'font/otf', '.woff': 'font/woff', '.woff2': 'font/woff2', '.wav': 'audio/wav', '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.txt': 'text/plain; charset=utf-8',
};

function serve(rootDir) {
  const server = http.createServer((req, res) => {
    const p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (p === '/favicon.ico') { res.writeHead(204); return res.end(); }
    const file = path.join(rootDir, p === '/' ? pagePath : p);
    if (!file.startsWith(rootDir)) { res.writeHead(403); return res.end(); }
    fs.stat(file, (err, st) => {
      if (err || !st.isFile()) { res.writeHead(404); return res.end('not found'); }
      const type = MIME[path.extname(file).toLowerCase()] || 'application/octet-stream';
      const range = req.headers.range;   // 미리보기 오디오 탐색(seek)용
      if (range) {
        const [a, b] = range.replace('bytes=', '').split('-');
        const start = Number(a), end = b ? Number(b) : st.size - 1;
        res.writeHead(206, { 'Content-Type': type, 'Content-Range': `bytes ${start}-${end}/${st.size}`, 'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1 });
        return fs.createReadStream(file, { start, end }).pipe(res);
      }
      res.writeHead(200, { 'Content-Type': type, 'Content-Length': st.size, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store' });
      fs.createReadStream(file).pipe(res);
    });
  });
  return new Promise(r => server.listen(Number(opt.port || 0), '127.0.0.1', () => r(server)));
}

async function launch(W, H) {
  const args = ['--force-color-profile=srgb', '--hide-scrollbars', '--font-render-hinting=none', '--disable-lcd-text',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
    `--window-size=${W},${H}`, '--autoplay-policy=no-user-gesture-required', '--mute-audio'];
  const exe = process.env.REEL_CHROME_PATH;
  const tries = exe ? [{ executablePath: exe }] : [{ channel: process.env.REEL_CHROME_CHANNEL || 'chrome' }, { channel: 'chromium' }, {}];
  let last;
  for (const t of tries) {
    try { return await chromium.launch({ headless: true, args, ...t }); } catch (e) { last = e; }
  }
  console.error('브라우저를 띄우지 못했다. Google Chrome 을 설치하거나 REEL_CHROME_PATH 로 Chromium 실행 파일을 지정하라.\n' + last);
  process.exit(1);
}

const pageErrors = [];
async function openPage(browser, url, W, H) {
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: scale });
  const page = await ctx.newPage();
  page.on('pageerror', e => { pageErrors.push(String(e)); console.error('[pageerror]', String(e)); });
  page.on('console', m => { if (m.type() === 'error' || (m.type() === 'warning' && m.text().startsWith('[REEL]'))) console.error('[console]', m.text()); });
  await page.goto(url + '?render=1', { waitUntil: 'load' });
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 120000 });
  const cdp = await ctx.newCDPSession(page);
  return { page, ctx, cdp };
}

async function shot(p, t, W, H, fmt) {
  await p.page.evaluate(tt => window.__seek(tt), t);
  const { data } = await p.cdp.send('Page.captureScreenshot', {
    format: fmt, ...(fmt === 'jpeg' ? { quality } : {}), optimizeForSpeed: true, captureBeyondViewport: false,
    clip: { x: 0, y: 0, width: W, height: H, scale: 1 },
  });
  return Buffer.from(data, 'base64');
}

// 프레임 → 최종 규격(H.264, yuv420p, BT.709, tv 범위). 모션 블러는 RGB 에서 평균한다.
function encoder(out, fps) {
  const inCodec = format === 'png' ? 'png' : 'mjpeg';
  const conv = format === 'png' ? 'scale=out_color_matrix=bt709:out_range=tv'
    : 'scale=in_range=pc:in_color_matrix=bt601:out_color_matrix=bt709:out_range=tv';
  const blur = sub > 1 ? `format=gbrp,tmix=frames=${sub},select='not(mod(n+1\\,${sub}))',setpts=N/(${fps}*TB),` : '';
  const vf = `${blur}${conv},format=yuv420p`;
  return spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-c:v', inCodec, '-framerate', String(fps * sub), '-i', '-',
    '-vf', vf, '-r', String(fps), '-c:v', 'libx264', '-preset', preset, '-crf', crf, '-pix_fmt', 'yuv420p',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
}

function run(cmd, args) {
  const r = spawnSync(cmd, args, { stdio: ['ignore', 'inherit', 'inherit'] });
  if (r.status !== 0) throw new Error(`${cmd} 종료 코드 ${r.status}`);
}

const server = await serve(root);
const url = `http://127.0.0.1:${server.address().port}${pagePath}`;

if (opt.serve) {
  console.log(`미리보기: ${url}\n(스페이스 재생 · ←→ 1초 · Shift+←→ 1프레임 · M 소리 · ?t=초) — Ctrl+C 로 종료`);
  await new Promise(() => {});
}

// 메타 정보(길이·해상도) 먼저 읽기
const probeBrowser = await launch(1920, 1080);
const probe = await openPage(probeBrowser, url, 1920, 1080);
const meta = await probe.page.evaluate(() => window.__meta);
await probeBrowser.close();
const W = meta.width, H = meta.height;
console.log(`페이지: ${meta.width}x${meta.height} ${meta.fps}fps ${meta.duration}s · 장면 ${meta.scenes.length}개 · 폰트 ${meta.fonts.join(', ') || '(없음!)'}`);
if (!meta.fonts.length) console.error('경고: 로드된 웹폰트가 없다. fonts/fonts.css 와 폰트 파일을 확인하라(시스템 폰트로 대체되면 다른 장비에서 화면이 달라진다).');

const browser = await launch(W, H);
const t0 = Date.now();
try {
  if (opt.stills || opt.every) {
    const outDir = path.resolve(opt.outdir || opt['stills-dir'] || path.join(root, 'qa', 'stills'));
    fs.mkdirSync(outDir, { recursive: true });
    let times = opt.stills ? opt.stills.split(',').map(Number) : [];
    if (opt.every) {
      const st = Number(opt.every), a = Number(opt.from || 0), b = Number(opt.to || meta.duration);
      for (let x = a; x < b - 1e-6; x += st) times.push(Number(x.toFixed(3)));
    }
    const n = Math.min(workers, times.length);
    const queue = [...times];
    const files = [];
    await Promise.all(Array.from({ length: n }, async () => {
      const p = await openPage(browser, url, W, H);
      while (queue.length) {
        const t = queue.shift();
        const buf = await shot(p, t, W, H, 'png');
        const file = path.join(outDir, `t${t.toFixed(2).padStart(6, '0')}.png`);
        fs.writeFileSync(file, buf);
        files.push(file);
      }
      await p.ctx.close();
    }));
    files.sort();
    console.log(`스틸 ${files.length}장 → ${outDir} (${((Date.now() - t0) / 1000).toFixed(1)}초)`);
  } else {
    const fps = Number(opt.fps || meta.fps || 30);
    const from = Number(opt.from || 0), to = Number(opt.to || meta.duration);
    const f0 = Math.round(from * fps), f1 = Math.round(to * fps);
    const out = path.resolve(opt.out || path.join(root, 'out', 'video.mp4'));
    fs.mkdirSync(path.dirname(out), { recursive: true });
    const segDir = out.replace(/\.mp4$/, '') + '.parts';
    if (opt.fresh && fs.existsSync(segDir)) fs.rmSync(segDir, { recursive: true });
    fs.mkdirSync(segDir, { recursive: true });
    // 장면 코드·자원이 바뀌면 이전 조각을 버린다(멈춘 렌더를 이어 갈 때 옛 코드와 새 코드 조각이 섞이지 않게)
    const code = (() => {
      const h = crypto.createHash('sha1');
      const walk = d => {
        for (const n of fs.readdirSync(d).sort()) {
          const f = path.join(d, n), st = fs.statSync(f);
          if (st.isDirectory()) { if (n !== 'fonts' && !n.startsWith('.')) walk(f); }
          else if (/\.(js|html|css|json)$/.test(n)) h.update(n).update(fs.readFileSync(f));
          else h.update(n + st.size + st.mtimeMs);
        }
      };
      walk(dir);
      return h.digest('hex').slice(0, 16);
    })();
    const sig = JSON.stringify({ scale, sub, shutter, format, crf, fps, W, H, code });
    const sigFile = path.join(segDir, 'settings.json');
    if (fs.existsSync(sigFile) && fs.readFileSync(sigFile, 'utf8') !== sig) {
      console.log('설정이나 장면 코드가 바뀌어 이전 조각을 버리고 처음부터 렌더한다');
      for (const f of fs.readdirSync(segDir)) fs.unlinkSync(path.join(segDir, f));   // 설정·코드가 바뀌면 이전 조각 폐기
    }
    fs.writeFileSync(sigFile, sig);
    const chunkF = Math.max(1, Math.round(Number(opt.chunk || 2) * fps));
    const chunks = [];
    for (let a = f0; a < f1; a += chunkF) chunks.push([a, Math.min(f1, a + chunkF)]);
    const segName = ([a, b]) => path.join(segDir, `seg_${String(a).padStart(6, '0')}_${String(b).padStart(6, '0')}.mp4`);
    const todo = chunks.filter(c => !fs.existsSync(segName(c)));
    if (todo.length < chunks.length) console.log(`이어서 렌더: ${chunks.length - todo.length}/${chunks.length} 청크는 이미 있음`);
    let done = chunks.length - todo.length;
    const n = Math.min(workers, todo.length);
    await Promise.all(Array.from({ length: n }, async () => {
      const p = await openPage(browser, url, W, H);
      while (todo.length) {
        const c = todo.shift();
        const tmp = segName(c).replace(/\.mp4$/, '.part.mp4');
        const ff = encoder(tmp, fps);
        for (let f = c[0]; f < c[1]; f++) {
          for (let k = 0; k < sub; k++) {
            const off = sub > 1 ? ((k + 0.5) / sub - 0.5) * shutter : 0;
            const t = Math.max(0, Math.min(to - 1e-4, (f + off) / fps));
            const buf = await shot(p, t, W, H, format);
            if (!ff.stdin.write(buf)) await once(ff.stdin, 'drain');
          }
        }
        ff.stdin.end();
        const [code] = await once(ff, 'close');
        if (code !== 0) throw new Error(`ffmpeg 실패(청크 ${c[0]}-${c[1]})`);
        fs.renameSync(tmp, segName(c));
        done++;
        process.stdout.write(`[${done}/${chunks.length}] ${(c[1] / fps).toFixed(1)}s\n`);
      }
      await p.ctx.close();
    }));
    const list = path.join(segDir, 'concat.txt');
    fs.writeFileSync(list, chunks.map(c => `file '${segName(c).split(path.sep).join('/')}'`).join('\n'));
    run('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', out]);
    const secs = (Date.now() - t0) / 1000;
    console.log(`완료: ${out} (${secs.toFixed(1)}초, ${f1 - f0}프레임 × 서브 ${sub}, ${fps}fps, ${W * scale}x${H * scale}, 워커 ${n || 0})`);
    if (!opt.keep) fs.rmSync(segDir, { recursive: true });
  }
} finally {
  await browser.close();
  server.close();
}
if (pageErrors.length) {
  console.error(`페이지 오류 ${pageErrors.length}건 — 해당 장면의 코드를 고치고 결과물을 확인하라.`);
  process.exit(2);
}
