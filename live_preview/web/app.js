import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';

// 애드온이 띄운 주소에서 열면 같은 출처, 다른 곳(배포 사이트)에서 열면 이 PC 의 애드온으로 접속
const API = new URLSearchParams(location.search).get('api') ?? (location.port === '8765' ? '' : 'http://127.0.0.1:8765');
const $ = id => document.getElementById(id);
const send = body => fetch(API + '/api/cmd', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).catch(() => {});

// ================= 상태 =================
let S = null;                 // 블렌더 상태 (state 이벤트)
let checking = new Set();     // 확인·렌더 대기 중인 프레임
// 프레임 → { v, bmp, bytes }: 압축을 미리 풀어 둔 그림(ImageBitmap). 재생 중 그릴 때 압축 풀기로 멈칫하지 않게.
// 새 렌더가 오기 전까지 이전 것을 유지한다
const imgs = new Map();
const loadingV = new Map();   // 프레임 → 불러오는 중인 버전
// 압축 푼 그림을 들고 있을 최대 메모리(바이트). 장면 전체가 들어가면 재생 중 새로 풀 일이 없어 끊기지 않는다.
// 넘으면 재생 위치에서 먼 것부터 내려놓음. 크롬은 PC 메모리를 8GB 까지만 알려 주는 경우가 많아 8 은 '8GB 이상'으로 본다
const BUDGET = (dm => (dm > 8 ? Math.min(4e9, dm * 1e9 / 8) : dm === 8 ? 2.5e9 : Math.max(5e8, dm * 1e9 / 8)))(navigator.deviceMemory || 8);
const AHEAD = 96;             // 재생 위치 앞으로 미리 준비해 둘 프레임 수
let decoded = 0;
let frame = 1, playing = false, inF = null, outF = null;
let need3d = true;            // 3D 화면은 무언가 바뀌었을 때만 다시 그린다 (매 화면 그리면 영상 재생과 자원을 다툼)

// ================= 서버 연결 =================
const es = new EventSource(API + '/events');
es.onopen = () => $('conn').classList.add('on');
es.onerror = () => $('conn').classList.remove('on');   // EventSource 가 알아서 재접속
es.addEventListener('state', e => onState(JSON.parse(e.data)));
es.addEventListener('frame', e => {
  if (!S) return;
  const { f, v } = JSON.parse(e.data);
  checking.delete(f);
  if (v) S.frames[f] = v;   // 그림은 prefetch 가 동시 개수 제한을 지키며 받아 온다
  if (f === frame) draw(); else queueBar();   // 현재 프레임이면 '갱신 중' 표시도 바로 지운다
  status();
});
es.addEventListener('cur', e => {
  const { f } = JSON.parse(e.data);
  if (S) S.frame = f;
  if ($('follow').checked && !playing && S && f !== frame) { frame = f; draw(); }
});
es.addEventListener('xf', e => applyXf(JSON.parse(e.data)));
es.addEventListener('glb', e => loadGlb(JSON.parse(e.data).v));

function onState(d) {
  if (d.start === undefined) return;             // 애드온이 아직 초기화 전
  const first = !S;
  if (!first && d.dir !== S.dir) { for (const f of [...imgs.keys()]) drop(f); loadingV.clear(); }   // 다른 파일의 그림이 남지 않게
  for (const f of [...imgs.keys()]) if (!(f in d.frames)) { drop(f); loadingV.delete(f); }
  S = d;
  checking = new Set(d.checking);
  barDirty = true;
  $('file').textContent = d.file;
  $('file').title = `미리보기 버전 ${d.version}`;
  $('engine').value = d.engine; $('res').value = d.res; $('auto').checked = d.auto;
  if (inF !== null && (inF < d.start || inF > d.end)) inF = null;
  if (outF !== null && (outF < d.start || outF > d.end)) outF = null;
  if (first || ($('follow').checked && !playing)) frame = d.frame;
  frame = Math.min(Math.max(frame, d.start), d.end);
  if (d.glb && d.glb !== glbV) loadGlb(d.glb);
  prefetch();
  status(); draw();
}

let notice = '', noticeTimer = 0;
function say(msg) {   // 작업을 막는 alert 대신 상단에 잠깐 표시
  notice = msg; clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice = ''; status(); }, 4000);
  status();
}

function status() {
  const el = $('status');
  if (notice) { el.className = 'err'; el.textContent = '⚠ ' + notice; return; }
  if (!S) return;
  el.className = S.error ? 'err' : '';
  el.textContent = S.error ? '⚠ ' + S.error
    : checking.size ? `렌더 중… 남은 확인 ${checking.size}프레임` : (S.auto ? '최신 상태' : '자동 렌더 꺼짐');
}

// 워커의 파일 이름 f"{f:05d}" 와 같게 (음수는 부호 포함 5자리: -0005)
const frameName = f => f < 0 ? '-' + String(-f).padStart(4, '0') : String(f).padStart(5, '0');
const frameUrl = (f, v) => `${API}/cache/frames/${frameName(f)}.jpg?v=${v}`;   // 버전마다 주소가 달라 브라우저 캐시가 안전
// 그림 받기·풀기는 작업 스레드 여러 개가 한다 (화면 쪽은 그리기만 → 풀기가 몰려도 재생이 끊기지 않게).
// box 를 주면 화면에 보이는 크기로 줄여서 준다: 그릴 때 축소 비용이 없고 메모리도 적다 (1080p → 화면 크기).
// 줄일 폭은 작업 스레드가 원본 크기를 보고 정한다 (해상도·비율이 바뀌어도 정확, 찌그러지지 않음)
const WORKER_SRC = `onmessage = async e => {
  const { id, url, box } = e.data;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    let m = await createImageBitmap(await r.blob());
    const s = box ? Math.min(box.w / m.width, box.h / m.height) : 1;
    if (s < 0.95) {
      const small = await createImageBitmap(m, { resizeWidth: Math.max(1, Math.round(m.width * s)),
                                                 resizeHeight: Math.max(1, Math.round(m.height * s)), resizeQuality: 'high' });
      m.close(); m = small;
    }
    postMessage({ id, m }, [m]);
  } catch (err) { postMessage({ id, err: String(err) }); }
};`;
let POOL = [];
try {
  const url = URL.createObjectURL(new Blob([WORKER_SRC], { type: 'text/javascript' }));
  POOL = Array.from({ length: Math.max(2, Math.min(4, (navigator.hardwareConcurrency || 4) >> 2)) }, () => new Worker(url));
} catch {}   // 작업 스레드를 못 쓰는 환경이면 화면 쪽에서 직접
const waiting = new Map();   // id → { done, w(맡긴 작업 스레드), timer }
let seq = 0;
function settle(id, d) {
  const job = waiting.get(id);
  if (!job) return;
  waiting.delete(id); clearTimeout(job.timer); job.done(d);
}
POOL.forEach(w => {
  w.onmessage = e => settle(e.data.id, e.data);
  // 작업 스레드가 죽으면(메모리 부족·보안 정책) 맡긴 일을 실패 처리하고 빼낸다 → 남은 스레드나 화면 쪽이 이어받음
  w.onerror = () => {
    POOL = POOL.filter(x => x !== w);
    for (const [id, job] of [...waiting]) if (job.w === w) settle(id, { err: 'worker died' });
  };
});
async function decodeOnPage(url, box) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const m = await createImageBitmap(await r.blob());
  const s = box ? Math.min(box.w / m.width, box.h / m.height) : 1;
  if (s >= 0.95) return m;
  const small = await createImageBitmap(m, { resizeWidth: Math.max(1, Math.round(m.width * s)), resizeHeight: Math.max(1, Math.round(m.height * s)), resizeQuality: 'high' });
  m.close();
  return small;
}
function decodeFrame(f, v, box) {
  const url = new URL(frameUrl(f, v), location.href).href;
  if (!POOL.length) return decodeOnPage(url, box);
  return new Promise((ok, fail) => {
    const id = seq++, w = POOL[id % POOL.length];
    // 응답이 15초 안에 없으면 실패로 보고 다시 시도하게 (대기열이 막혀 미리 받기가 멈추지 않게)
    const timer = setTimeout(() => settle(id, { err: 'timeout' }), 15000);
    waiting.set(id, { w, timer, done: d => (d.m ? ok(d.m) : fail(new Error(d.err))) });
    w.postMessage({ id, url, box });
  });
}
let decodeBox = null;   // 지금 준비해 둔 그림들이 맞춰진 화면 크기
const viewBox = () => (view.width > 0 ? { w: view.width, h: view.height } : null);
const sameBox = (x, y) => x === y || (x && y && x.w === y.w && x.h === y.h);

async function loadImg(f, v) {
  if (imgs.get(f)?.v === v || loadingV.get(f) === v) return;
  loadingV.set(f, v);
  try {
    decodeBox ??= viewBox();
    const box = decodeBox;
    const bmp = await decodeFrame(f, v, box);
    if (loadingV.get(f) !== v) { bmp.close(); return; }   // 그사이 더 새 버전이 옴
    loadingV.delete(f);
    drop(f);
    const bytes = bmp.width * bmp.height * 4;
    // 받는 사이 화면 크기가 바뀌었으면 '~' 표시로 새 크기로 다시 받게 (그때까지는 이 그림을 씀)
    imgs.set(f, { v: sameBox(box, decodeBox) ? v : v + '~', bmp, bytes, width: bmp.width, height: bmp.height });
    decoded += bytes;
    trim();
    if (f === frame) draw();
  } catch {
    if (loadingV.get(f) === v) loadingV.delete(f);   // 실패하면 다음에 다시 시도
  }
}
function drop(f) {
  const e = imgs.get(f);
  if (!e) return;
  imgs.delete(f); decoded -= e.bytes; e.bmp.close();
}
// 재생 방향 기준 거리: 반복 재생이면 구간 끝 다음은 구간 처음 (미리 받기·메모리 정리가 같은 기준을 써야 진동하지 않음)
function ahead(f) {
  const [a, b] = range(), len = b - a + 1;
  if ($('loop').checked && f >= a && f <= b && frame >= a && frame <= b) return ((f - frame) % len + len) % len;
  return f - frame;
}
const BEHIND = 24;   // 재생 위치 뒤로 남겨 둘 프레임 수 (앞뒤로 훑을 때)
// 재생 위치 주변(뒤 BEHIND, 앞 AHEAD)을 미리 준비한다. 재생 중엔 매 화면마다 불려 앞쪽을 계속 채운다
function prefetch() {
  if (!S || !view.width) return;   // 렌더 칸이 안 보이면(3D만 보기) 쉰다
  const [a, b] = range();
  const order = [frame];
  for (let i = 1; i <= AHEAD; i++) { order.push(frame + i); if (i <= BEHIND) order.push(frame - i); }
  let started = loadingV.size;
  for (let f of order) {
    if (started >= 4) break;          // 동시에 푸는 개수를 제한 (한꺼번에 몰리면 그 순간 화면이 끊김)
    if (f > b && $('loop').checked) f = a + (f - b - 1);   // 반복 재생이면 구간 처음으로 이어서
    if (f < S.start || f > S.end) continue;
    const v = S.frames[f];
    if (v && imgs.get(f)?.v !== v && loadingV.get(f) !== v) { loadImg(f, v); started++; }
  }
}
// 화면(렌더 칸)이 준비해 둔 그림보다 눈에 띄게 커지면 새 크기로 다시 준비 (작아지는 건 그대로 줄여 그림).
// 지금 그림은 버리지 않고 '~' 표시만 → 재생은 기존 그림으로 계속되고 하나씩 새 크기로 바뀐다 (끊김 없음)
function refit() {
  const nb = viewBox();
  if (!nb) return;
  if (decodeBox && nb.w <= decodeBox.w * 1.2 && nb.h <= decodeBox.h * 1.2) return;
  decodeBox = nb;
  for (const e of imgs.values()) if (!e.v.endsWith('~')) e.v += '~';
  prefetch();
}
// 메모리 상한을 넘으면 내려놓는다: 미리 받기 범위(뒤 BEHIND·앞 AHEAD) 밖에서 먼 것부터,
// 그래도 넘으면 범위 안의 먼 앞쪽부터 (바로 앞 8장은 끝까지 지킴)
function trim() {
  if (decoded <= BUDGET) return;
  const keep = f => { const d = ahead(f); return d >= -BEHIND && d <= AHEAD; };
  const order = [...imgs.keys()].sort((x, y) => (keep(x) - keep(y)) || (Math.abs(ahead(y)) - Math.abs(ahead(x))));
  for (const f of order) {
    if (decoded <= BUDGET * 0.9) break;
    const d = ahead(f);
    if (d >= 0 && d <= 8) continue;
    drop(f);
  }
}

// ================= 렌더 영상 재생 =================
const view = $('view'), vctx = view.getContext('2d');
const bar = $('bar'), bctx = bar.getContext('2d');

function fit(c) {   // 크기가 바뀌었으면 true (캔버스가 비워짐)
  const r = devicePixelRatio, w = Math.round(c.clientWidth * r), h = Math.round(c.clientHeight * r);
  if (c.width === w && c.height === h) return false;
  c.width = w; c.height = h;
  return true;
}

function stateOf(f) {
  if (S.frames[f] && !checking.has(f)) return 'done';
  return S.frames[f] ? 'stale' : 'none';
}

let shown = false;
function draw() {
  if (!S) return;
  const resized = fit(view);
  const e = imgs.get(frame);
  if (e) {
    vctx.fillStyle = '#111'; vctx.fillRect(0, 0, view.width, view.height);
    const s = Math.min(view.width / e.width, view.height / e.height), w = e.width * s, h = e.height * s;
    vctx.drawImage(e.bmp, (view.width - w) / 2, (view.height - h) / 2, w, h);
    shown = true;
  } else if (!playing || resized || !shown) {
    // 재생 중엔 아직 준비 안 된 프레임에서 안내 문구를 번쩍이지 않고 직전 그림을 잠깐 유지한다
    vctx.fillStyle = '#111'; vctx.fillRect(0, 0, view.width, view.height);
    vctx.fillStyle = '#777'; vctx.font = `${16 * devicePixelRatio}px sans-serif`; vctx.textAlign = 'center';
    vctx.fillText(S.frames[frame] ? '불러오는 중…' : '이 프레임은 렌더 대기 중', view.width / 2, view.height / 2);
    shown = false;
  }
  const st = { done: '', stale: ' (이전 렌더, 갱신 중)', none: ' (렌더 전)' }[stateOf(frame)];
  const label = `${frame}  /  ${S.start}–${S.end}${st}`;
  if (frameNo.textContent !== label) frameNo.textContent = label;
  drawBar();
}
const frameNo = $('frameNo');

const COLOR = { done: '#3a8f5a', stale: '#c08a2e', none: '#45464c' };
let barQueued = false, barDirty = true;
const barBase = document.createElement('canvas'), baseCtx = barBase.getContext('2d');
function queueBar() {   // 프레임 완료가 몰려 와도 바는 화면 갱신마다 한 번만 그린다
  barDirty = true;
  if (barQueued) return;
  barQueued = true;
  requestAnimationFrame(() => { barQueued = false; drawBar(); });
}
// 바 = 바탕(프레임 상태·구간·마커·눈금, 바뀔 때만 다시 그림) + 재생 위치선(매 프레임).
// 재생 중 매 프레임 수천 칸을 다시 그리면 긴 장면에서 화면이 끊긴다
function drawBar() {
  if (!S) return;
  if (fit(bar) || barDirty) { drawBarBase(); barDirty = false; }
  const r = devicePixelRatio, n = S.end - S.start + 1, fw = bar.width / n;
  bctx.drawImage(barBase, 0, 0);
  const x = (frame - S.start + 0.5) * fw;               // 현재 위치
  bctx.fillStyle = '#fff'; bctx.fillRect(x - r, 0, 2 * r, bar.height);
}
function drawBarBase() {
  barBase.width = bar.width; barBase.height = bar.height;
  const bctx = baseCtx;
  const r = devicePixelRatio, W = bar.width, H = bar.height, n = S.end - S.start + 1, fw = W / n;
  bctx.fillStyle = '#18191b'; bctx.fillRect(0, 0, W, H);
  for (let f = S.start; f <= S.end; f++) {            // 프레임별 렌더 상태
    bctx.fillStyle = COLOR[stateOf(f)];
    bctx.fillRect((f - S.start) * fw, H - 14 * r, Math.max(fw - (fw > 3 ? 1 : 0), 1), 10 * r);
  }
  if (inF !== null || outF !== null) {                 // 재생 구간
    const [a, b] = range();
    bctx.fillStyle = 'rgba(74,158,255,.18)'; bctx.fillRect((a - S.start) * fw, 0, (b - a + 1) * fw, H);
    bctx.fillStyle = '#4a9eff';
    bctx.fillRect((a - S.start) * fw, 0, 2 * r, H); bctx.fillRect((b - S.start + 1) * fw - 2 * r, 0, 2 * r, H);
  }
  bctx.font = `${11 * r}px sans-serif`; bctx.textAlign = 'left';
  for (const [f, name] of S.markers) {                 // 마커
    if (f < S.start || f > S.end) continue;
    const x = (f - S.start + 0.5) * fw;
    bctx.fillStyle = '#e8c547'; bctx.beginPath();
    bctx.moveTo(x - 4 * r, 0); bctx.lineTo(x + 4 * r, 0); bctx.lineTo(x, 6 * r); bctx.fill();
    bctx.fillText(name, x + 5 * r, 11 * r);
  }
  const step = Math.max(1, Math.ceil(n / (W / (40 * r))));   // 눈금 숫자
  bctx.fillStyle = '#777';
  for (let f = S.start; f <= S.end; f += step) bctx.fillText(f, (f - S.start) * fw + 2 * r, 26 * r);
}

const range = () => [inF ?? S.start, outF ?? S.end];

function setFrame(f, toBlender = true) {
  if (!S) return;
  frame = Math.min(Math.max(f, S.start), S.end);
  draw();
  if (toBlender && $('follow').checked) send({ op: 'frame', f: frame });
}

function step(d, auto = false) {
  const [a, b] = range();
  let f = frame + d;
  if (frame < a || frame > b) f = d > 0 ? a : b;   // 구간 밖에 있었으면 구간 안으로
  else if (f > b) { if (auto && !$('loop').checked) { setPlaying(false); return; } f = a; }
  else if (f < a) f = b;
  setFrame(f, !auto);
}

function setPlaying(p) {
  playing = p;
  $('play').textContent = p ? '⏸' : '▶';
  if (!p && S) setFrame(frame);   // 멈추면 블렌더도 그 프레임으로
  if (!p && glbPending) { const v = glbPending; glbPending = null; loadGlb(v); }   // 재생 중 미뤄 둔 3D 모델
}

let last = 0, acc = 0;
const speedSel = $('speed');
function tick(t) {
  if (playing && S) {
    acc += Math.min((t - last) / 1000, 0.25) * S.fps * +speedSel.value;
    let n = Math.floor(acc);
    acc -= n;
    if (n > 0) {   // 늦어졌으면 여러 프레임을 건너뛰되 그리기는 한 번만 (영상 시간이 실제 시간과 맞게)
      while (n-- > 1) advance();
      step(1, true);
    }
  } else acc = 0;
  last = t;
  prefetch();
  if (need3d) { need3d = false; renderThree(); }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
function advance() {   // 그리지 않고 한 프레임만 넘김
  const [a, b] = range();
  if (frame >= b) { if ($('loop').checked) frame = a; } else frame = Math.max(frame + 1, a);
}

function barFrame(e) {
  const r = bar.getBoundingClientRect();
  return S.start + Math.floor((e.clientX - r.left) / r.width * (S.end - S.start + 1));
}
bar.addEventListener('pointerdown', e => {
  if (!S) return;
  bar.setPointerCapture(e.pointerId);
  setFrame(barFrame(e), false);
  const move = ev => setFrame(barFrame(ev), false);
  bar.addEventListener('pointermove', move);
  bar.addEventListener('pointerup', () => { bar.removeEventListener('pointermove', move); setFrame(frame); }, { once: true });
});

$('play').onclick = () => setPlaying(!playing);
$('prev').onclick = () => step(-1);
$('next').onclick = () => step(1);
$('first').onclick = () => setFrame(range()[0]);
$('last').onclick = () => setFrame(range()[1]);
$('setIn').onclick = () => { inF = frame; if (outF !== null && outF < inF) outF = null; queueBar(); };
$('setOut').onclick = () => { outF = frame; if (inF !== null && inF > outF) inF = null; queueBar(); };
$('clearRange').onclick = () => { inF = outF = null; queueBar(); };
$('follow').onchange = e => { if (e.target.checked && S) setFrame(S.frame, false); };
$('engine').onchange = $('res').onchange = () => send({ op: 'settings', engine: $('engine').value, res: +$('res').value });
$('auto').onchange = e => send({ op: 'settings', auto: e.target.checked });
$('rerender').onclick = () => send({ op: 'rerender' });
$('layout').onchange = e => { $('main').className = e.target.value; setTimeout(() => { draw(); refit(); need3d = true; }); };
addEventListener('resize', () => { draw(); refit(); need3d = true; });

// 구간(없으면 전체)을 영상 파일로 저장. 브라우저 내장 MediaRecorder 사용
$('export').onclick = async () => {
  if (!S) return;
  const [a, b] = range();
  const list = [];   // 시작할 때의 프레임·버전을 떠 둔다: 저장 중에 새 렌더·파일 전환이 와도 섞이지 않게
  for (let f = a; f <= b; f++) {
    if (!S.frames[f]) return say(`${f}프레임이 아직 렌더되지 않았습니다.`);
    if (checking.has(f)) return say('렌더가 끝난 뒤 저장해 주세요 (갱신 중인 프레임이 있음).');
    list.push([f, S.frames[f]]);
  }
  const fps = S.fps, name = `${S.file.replace(/\.blend$/, '')}_${a}-${b}`;
  const btn = $('export'); btn.disabled = true; btn.textContent = '저장 중… (이 탭을 띄워 두세요)';
  // 한 장씩 불러 그린다 (긴 구간도 메모리에 다 올리지 않음).
  // 화면용 그림은 화면 크기로 줄여 둔 것이라, 저장은 항상 원본 크기로 따로 푼다
  const get = async ([f, v]) => ({ bmp: await decodeFrame(f, v), own: true });
  let rec = null, track = null;
  try {
    const c = document.createElement('canvas');
    const first = await get(list[0]);
    c.width = first.bmp.width; c.height = first.bmp.height;
    if (first.own) first.bmp.close();
    const x = c.getContext('2d');
    const type = ['video/mp4;codecs=avc1', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm'].find(t => MediaRecorder.isTypeSupported(t));
    const stream = c.captureStream(0);
    track = stream.getVideoTracks()[0];
    rec = new MediaRecorder(stream, { mimeType: type, videoBitsPerSecond: 8e6 });
    const chunks = [];
    rec.ondataavailable = e => chunks.push(e.data);
    const stopped = new Promise(r => (rec.onstop = r));
    rec.start();
    let hidden = false;
    const t0 = performance.now();
    for (let i = 0; i < list.length; i++) {
      const { bmp, own } = await get(list[i]);
      x.drawImage(bmp, 0, 0, c.width, c.height);
      if (own) bmp.close();
      track.requestFrame();
      hidden ||= document.hidden;   // 탭이 가려지면 브라우저가 타이머를 늦춰 영상 타이밍이 어긋난다
      // 정해진 시각표에 맞춰 기다린다 (불러오기에 걸린 시간만큼 덜 기다려 영상 길이가 정확)
      await new Promise(r => setTimeout(r, Math.max(0, t0 + (i + 1) * 1000 / fps - performance.now())));
    }
    rec.stop(); await stopped;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob(chunks, { type }));
    link.download = `${name}.${type.startsWith('video/mp4') ? 'mp4' : 'webm'}`;
    link.click();
    if (hidden) say('저장 중 탭이 가려져 영상 속도가 어긋났을 수 있습니다. 탭을 띄운 채 다시 저장해 주세요.');
  } catch (e) {
    // 서버는 지금 버전의 그림만 준다 → 저장 도중 그 구간이 다시 렌더되면 여기로 온다
    say(String(e.message).includes('404') ? '저장 중에 렌더가 바뀌었습니다. 렌더가 끝난 뒤 다시 저장해 주세요.' : '영상 저장 실패: ' + e.message);
  } finally {
    if (rec && rec.state !== 'inactive') rec.stop();   // 실패해도 녹화기·영상 트랙을 남기지 않는다
    track?.stop();
    btn.disabled = false; btn.textContent = '영상 저장';
  }
};

// ================= 3D 구도 조절 (three.js) =================
// 블렌더(Z-up) ↔ three(Y-up). glTF 익스포터와 같은 변환이라 오브젝트는 C·M·C⁻¹, 카메라는 C·M
const C = new THREE.Matrix4().set(1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1);
const Ci = C.clone().invert();
const box = $('three');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(devicePixelRatio);
box.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x2b2b2b);
scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2));
const sun = new THREE.DirectionalLight(0xffffff, 1.5);
sun.position.set(5, 10, 7);
scene.add(sun, new THREE.GridHelper(20, 20, 0x555555, 0x3a3a3a));

const freeCam = new THREE.PerspectiveCamera(50, 1, 0.01, 2000);
freeCam.position.set(8, 6, 8);
const camProxy = new THREE.PerspectiveCamera(40, 16 / 9, 0.01, 2000);   // 블렌더 장면 카메라
const camPick = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.3, 0.5), new THREE.MeshBasicMaterial({ color: 0xffaa33, wireframe: true }));
camProxy.add(camPick);
const camHelper = new THREE.CameraHelper(camProxy);
scene.add(camProxy, camHelper);
camProxy.visible = camHelper.visible = false;

const orbit = new OrbitControls(freeCam, renderer.domElement);
const gizmo = new TransformControls(freeCam, renderer.domElement);
scene.add(gizmo.getHelper());
orbit.addEventListener('change', () => { need3d = true; });
gizmo.addEventListener('change', () => { need3d = true; });   // 기즈모 hover·드래그

let model = null, glbV = '', lastXf = null, camMode = false, holdCam = 0;
const byName = new Map();       // 블렌더 오브젝트 이름 → three 오브젝트

// 3D 화면용 모델은 Draco 로 압축돼 온다. 압축 풀기는 DRACOLoader 가 작업 스레드에서 한다 (영상 재생이 멈칫하지 않게)
const gltfLoader = new GLTFLoader().setDRACOLoader(new DRACOLoader().setDecoderPath('./vendor/draco/'));
let glbPending = null;
function loadGlb(v) {
  glbV = v;
  // 드래그 도중 바꿔 끼우면 기즈모가 튀고, 영상 재생 중이면 큰 모델을 그래픽카드에 올리는 순간 영상이 멈칫한다
  // → 놓거나 재생을 멈춘 뒤에
  if (gizmo.dragging || playing) { glbPending = v; return; }
  gltfLoader.load(`${API}/cache/scene.glb?v=${v}`, g => {
    if (v !== glbV) return;
    if (gizmo.dragging || playing) { glbPending = v; return; }
    const sel = gizmo.object?.userData.bname;
    if (model) {   // 옛 모델의 GPU 메모리(형태·재질·텍스처)를 놓아 준다: 편집할 때마다 새 모델이 오므로
      scene.remove(model);
      model.traverse(o => {
        o.geometry?.dispose();
        for (const m of [o.material].flat()) {
          if (!m) continue;
          for (const k in m) if (m[k]?.isTexture) m[k].dispose();
          m.dispose();
        }
      });
    }
    model = g.scene;
    byName.clear();
    model.traverse(o => { if (o.userData.name) { o.userData.bname = o.userData.name; byName.set(o.userData.name, o); } });
    scene.add(model);
    need3d = true;
    if (lastXf) applyXf(lastXf);
    if (sel && sel !== camProxy.userData.bname) byName.has(sel) ? select(byName.get(sel)) : select(null);
  });
}

function vfov(c, aspect) {
  const fit = c.fit === 'AUTO' ? (aspect >= 1 ? 'HORIZONTAL' : 'VERTICAL') : c.fit;
  const sensor = c.fit === 'VERTICAL' ? c.sensor_h : c.sensor;
  let half = Math.atan(sensor / 2 / c.lens);
  if (fit === 'HORIZONTAL') half = Math.atan(Math.tan(half) / aspect);
  return THREE.MathUtils.radToDeg(half * 2);
}

const depth = o => { let n = 0; while ((o = o.parent)) n++; return n; };
const setWorld = (o, w) => {
  o.parent.updateWorldMatrix(true, false);
  o.matrix.copy(o.parent.matrixWorld).invert().multiply(w);
  o.matrix.decompose(o.position, o.quaternion, o.scale);
};

function applyXf(d) {
  if (!d.obs) return;   // 애드온이 막 켜져 아직 보낼 값이 없을 때
  lastXf = d;
  need3d = true;
  const dragging = gizmo.dragging ? gizmo.object : null;
  if (model) {
    const items = [];
    for (const [name, m] of Object.entries(d.obs)) {
      const o = byName.get(name);
      // 끌고 있는 오브젝트와 그 자식은 블렌더에서 늦게 오는 값으로 덮지 않는다 (떨림)
      let held = false;
      for (let p = o; p && dragging; p = p.parent) if (p === dragging) { held = true; break; }
      if (o && !held) items.push([o, C.clone().multiply(new THREE.Matrix4().set(...m)).multiply(Ci)]);
    }
    items.sort((a, b) => depth(a[0]) - depth(b[0]));   // 부모 먼저
    for (const [o, w] of items) setWorld(o, w);
  }
  camProxy.visible = !!d.cam;
  camHelper.visible = !!d.cam && !camMode;
  if (!d.cam) {   // 카메라가 지워짐
    if (camMode) setCamMode(false);
    if (gizmo.object === camProxy) select(null);
    return;
  }
  camProxy.userData.bname = d.cam.name;
  camProxy.userData.cam = d.cam;
  camProxy.aspect = d.aspect;
  if (document.activeElement !== $('lens')) { $('lens').value = d.cam.lens; $('lensVal').textContent = d.cam.lens.toFixed(0); }
  camProxy.fov = vfov(d.cam, d.aspect);
  camProxy.updateProjectionMatrix();
  if (dragging !== camProxy && performance.now() > holdCam) {
    setWorld(camProxy, C.clone().multiply(new THREE.Matrix4().set(...d.cam.m)));
    if (camMode) syncOrbitTarget();
  }
  camHelper.update();
}

// three → 블렌더 (드래그 중에는 80ms 간격, 끝날 때 한 번 더 확정)
let lastSend = 0;
function pushXf(o, end = false) {
  const now = performance.now();
  if (!o?.userData.bname || (!end && now - lastSend < 80)) return;
  lastSend = now;
  o.updateWorldMatrix(true, false);
  const mb = o === camProxy ? Ci.clone().multiply(o.matrixWorld) : Ci.clone().multiply(o.matrixWorld).multiply(C);
  send({ op: 'xform', name: o.userData.bname, m: mb.transpose().elements, end, key: end && $('keyOn').checked });
}

// 실제로 움직였을 때만 확정(키프레임·되돌리기 단계)을 보낸다: 클릭만 하거나 눌렀다 놓기만 한 경우 제외
let gizmoMoved = false;
gizmo.addEventListener('dragging-changed', e => {
  orbit.enabled = !e.value;
  if (!e.value && gizmoMoved) pushXf(gizmo.object, true);
  gizmoMoved = false;
  if (!e.value && glbPending) { const v = glbPending; glbPending = null; loadGlb(v); }
});
gizmo.addEventListener('objectChange', () => {
  gizmoMoved = true;
  pushXf(gizmo.object);
  if (gizmo.object === camProxy) camHelper.update();
});

function select(o) {
  need3d = true;
  if (o) gizmo.attach(o); else gizmo.detach();
  $('sel').textContent = o ? `선택: ${o.userData.bname}` : '오브젝트를 클릭해 선택';
}

// 클릭(드래그 아님)으로 선택
const ray = new THREE.Raycaster(), down = new THREE.Vector2();
renderer.domElement.addEventListener('pointerdown', e => down.set(e.clientX, e.clientY));
renderer.domElement.addEventListener('pointerup', e => {
  if (gizmo.dragging || down.distanceTo(new THREE.Vector2(e.clientX, e.clientY)) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  const vp = viewRect(r.width, r.height);
  ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left - vp.x) / vp.w) * 2 - 1, -((e.clientY - r.top - vp.y) / vp.h) * 2 + 1), camMode ? camProxy : freeCam);
  const targets = [...(model ? [model] : []), ...(camMode ? [] : [camPick])];
  const hit = ray.intersectObjects(targets, true).find(h => h.object.visible);
  let o = hit?.object;
  while (o && !o.userData.bname && o !== camPick) o = o.parent;
  select(o === camPick ? camProxy : o ?? null);
});

for (const b of document.querySelectorAll('[data-mode]')) b.onclick = () => setMode(b.dataset.mode);
function setMode(m) {
  gizmo.setMode(m);
  for (const b of document.querySelectorAll('[data-mode]')) b.classList.toggle('on', b.dataset.mode === m);
}

// 카메라 시점: 블렌더 카메라로 보고, 화면을 돌리면 블렌더 카메라가 움직인다
function syncOrbitTarget() {
  const dir = new THREE.Vector3(0, 0, -1).applyQuaternion(camProxy.getWorldQuaternion(new THREE.Quaternion()));
  const pos = camProxy.getWorldPosition(new THREE.Vector3());
  let dist = 5;
  if (model) {
    ray.set(pos, dir);
    const hit = ray.intersectObject(model, true)[0];
    if (hit) dist = hit.distance;
  }
  orbit.target.copy(pos).addScaledVector(dir, dist);
  // OrbitControls 는 돌릴 때마다 롤을 0으로 만든다 → 지금 롤을 기억했다가 change 때 다시 적용
  const flat = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().lookAt(pos, orbit.target, camProxy.up));
  const r = flat.invert().multiply(camProxy.getWorldQuaternion(new THREE.Quaternion()));
  camRoll = 2 * Math.atan2(r.z, r.w);
}
let camRoll = 0;
function setCamMode(on) {
  if (on && !camProxy.visible) return say('블렌더 장면에 카메라가 없습니다.');
  need3d = true;
  if (camEndTimer && camMode && !on) {   // 조작 직후 바로 나가도 확정(키프레임·되돌리기 단계)이 빠지지 않게
    clearTimeout(camEndTimer); camEndTimer = 0;
    pushXf(camProxy, true);
  }
  camMode = on;
  $('camView').classList.toggle('on', on);
  camPick.visible = !on; camHelper.visible = !on && camProxy.visible;
  if (gizmo.object === camProxy) select(null);
  orbit.object = gizmo.camera = on ? camProxy : freeCam;
  // 켤 때 update() 를 부르면 카메라가 target 을 향해 다시 돌면서 블렌더 카메라가 움직이므로 끌 때만
  if (on) syncOrbitTarget(); else { orbit.target.set(0, 0, 0); orbit.update(); }
}
$('camView').onclick = () => setCamMode(!camMode);
// 조작 중에는 블렌더에서 오는 카메라 값을 잠시 무시하고, 조작이 0.4초 멈추면 한 번만 확정한다
// (휠 한 칸마다, 클릭만 할 때마다 키프레임·되돌리기 단계가 쌓이지 않게)
let camEndTimer = 0;
orbit.addEventListener('change', () => {
  if (!camMode) return;
  camProxy.rotateZ(camRoll);
  holdCam = performance.now() + 500;
  pushXf(camProxy);
  clearTimeout(camEndTimer);
  camEndTimer = setTimeout(() => {
    camEndTimer = 0;
    if (camMode) { pushXf(camProxy, true); holdCam = performance.now() + 500; }
  }, 400);
});

$('lens').oninput = e => {
  const c = camProxy.userData.cam; if (!c) return;
  c.lens = +e.target.value; $('lensVal').textContent = c.lens;
  camProxy.fov = vfov(c, camProxy.aspect); camProxy.updateProjectionMatrix(); camHelper.update(); need3d = true;
  send({ op: 'lens', name: c.name, lens: c.lens });
};
$('lens').onchange = e => {
  const c = camProxy.userData.cam; if (!c) return;
  send({ op: 'lens', name: c.name, lens: +e.target.value, end: true, key: $('keyOn').checked });
  e.target.blur();
};

// 카메라 시점일 때는 렌더 비율에 맞춰 위아래/좌우를 검게 남긴다
function viewRect(w, h) {
  if (!camMode) return { x: 0, y: 0, w, h };
  const a = camProxy.aspect;
  let vw = w, vh = w / a;
  if (vh > h) { vh = h; vw = h * a; }
  return { x: (w - vw) / 2, y: (h - vh) / 2, w: vw, h: vh };
}
new ResizeObserver(() => { need3d = true; }).observe(box);   // 보기 전환·창 크기 등 3D 칸 크기가 바뀌면
function renderThree() {
  if (box.clientWidth === 0) return;
  const w = box.clientWidth, h = box.clientHeight;
  const c = renderer.domElement;
  if (c.clientWidth !== w || c.clientHeight !== h) renderer.setSize(w, h);
  const vp = viewRect(w, h);
  renderer.setScissorTest(true);
  renderer.setScissor(0, 0, w, h); renderer.setClearColor(0x000000); renderer.clear();
  renderer.setViewport(vp.x, h - vp.y - vp.h, vp.w, vp.h); renderer.setScissor(vp.x, h - vp.y - vp.h, vp.w, vp.h);
  if (!camMode) { freeCam.aspect = w / h; freeCam.updateProjectionMatrix(); }
  renderer.render(scene, camMode ? camProxy : freeCam);
}

// ================= 단축키 =================
// 버튼·체크박스는 누른 뒤 포커스를 놓는다: 남아 있으면 Space 가 그 버튼을 다시 누르고 단축키가 막힌다
addEventListener('click', e => { if (e.target.matches('button, input[type=checkbox]')) e.target.blur(); });
addEventListener('change', e => { if (e.target.matches('select')) e.target.blur(); });   // 고른 뒤엔 단축키로 돌아오게
addEventListener('keydown', e => {
  if (e.target.matches('select, input[type=range], input[type=text]') || e.ctrlKey || e.metaKey || e.altKey) return;
  // e.code(자판 위치)를 쓴다: 한글 입력 상태에서는 e.key 가 'ㅎ'·'ㄱ' 등으로 와서 단축키가 안 먹는다
  const k = e.code;
  if (e.repeat && k === 'Space') return e.preventDefault();   // 길게 눌러도 재생·정지가 연타되지 않게
  if (k === 'Space') { e.preventDefault(); setPlaying(!playing); }
  else if (k === 'ArrowLeft') step(-1);
  else if (k === 'ArrowRight') step(1);
  else if (k === 'KeyI') $('setIn').click();
  else if (k === 'KeyO') $('setOut').click();
  else if (k === 'KeyG') setMode('translate');
  else if (k === 'KeyR') setMode('rotate');
  else if (k === 'KeyS') setMode('scale');
  else if (k === 'KeyC') setCamMode(!camMode);
  else if (k === 'Escape') select(null);
});
