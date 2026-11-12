import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// 애드온이 띄운 주소에서 열면 같은 출처, 다른 곳(배포 사이트)에서 열면 이 PC 의 애드온으로 접속
const API = new URLSearchParams(location.search).get('api') ?? (location.port === '8765' ? '' : 'http://127.0.0.1:8765');
const $ = id => document.getElementById(id);
const send = body => fetch(API + '/api/cmd', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).catch(() => {});

// ================= 상태 =================
let S = null;                 // 블렌더 상태 (state 이벤트)
let checking = new Set();     // 확인·렌더 대기 중인 프레임
const imgs = new Map();       // 프레임 → 화면에 쓸 이미지 (새 렌더가 오기 전까지 이전 것 유지)
const loadingV = new Map();   // 프레임 → 불러오는 중인 버전
let frame = 1, playing = false, inF = null, outF = null;

// ================= 서버 연결 =================
const es = new EventSource(API + '/events');
es.onopen = () => $('conn').classList.add('on');
es.onerror = () => $('conn').classList.remove('on');   // EventSource 가 알아서 재접속
es.addEventListener('state', e => onState(JSON.parse(e.data)));
es.addEventListener('frame', e => {
  if (!S) return;
  const { f, v } = JSON.parse(e.data);
  checking.delete(f);
  if (v) { S.frames[f] = v; loadImg(f, v); }
  drawBar(); status();
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
  if (!first && d.file !== S.file) { imgs.clear(); loadingV.clear(); }   // 다른 파일의 이미지가 남지 않게
  for (const f of imgs.keys()) if (!(f in d.frames)) imgs.delete(f);
  S = d;
  checking = new Set(d.checking);
  for (const [f, v] of Object.entries(d.frames)) loadImg(+f, v);
  $('file').textContent = d.file;
  $('file').title = `미리보기 버전 ${d.version}`;
  $('engine').value = d.engine; $('res').value = d.res; $('auto').checked = d.auto;
  if (inF !== null && (inF < d.start || inF > d.end)) inF = null;
  if (outF !== null && (outF < d.start || outF > d.end)) outF = null;
  if (first || ($('follow').checked && !playing)) frame = d.frame;
  frame = Math.min(Math.max(frame, d.start), d.end);
  if (d.glb && d.glb !== glbV) loadGlb(d.glb);
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

function loadImg(f, v) {
  if ((imgs.get(f)?.v ?? loadingV.get(f)) === v) return;
  loadingV.set(f, v);
  const im = new Image();
  im.onload = () => {
    if (loadingV.get(f) !== v) return;          // 그사이 더 새 버전이 옴
    im.v = v; imgs.set(f, im); loadingV.delete(f);
    if (f === frame) draw();
  };
  im.src = `${API}/cache/frames/${String(f).padStart(5, '0')}.jpg?v=${v}`;
}

// ================= 렌더 영상 재생 =================
const view = $('view'), vctx = view.getContext('2d');
const bar = $('bar'), bctx = bar.getContext('2d');

function fit(c) {
  const r = devicePixelRatio, w = Math.round(c.clientWidth * r), h = Math.round(c.clientHeight * r);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
}

function stateOf(f) {
  if (S.frames[f] && !checking.has(f)) return 'done';
  return S.frames[f] ? 'stale' : 'none';
}

function draw() {
  if (!S) return;
  fit(view);
  vctx.fillStyle = '#111'; vctx.fillRect(0, 0, view.width, view.height);
  const im = imgs.get(frame);
  if (im) {
    const s = Math.min(view.width / im.width, view.height / im.height), w = im.width * s, h = im.height * s;
    vctx.drawImage(im, (view.width - w) / 2, (view.height - h) / 2, w, h);
  } else {
    vctx.fillStyle = '#777'; vctx.font = `${16 * devicePixelRatio}px sans-serif`; vctx.textAlign = 'center';
    vctx.fillText('이 프레임은 렌더 대기 중', view.width / 2, view.height / 2);
  }
  const st = { done: '', stale: ' (이전 렌더, 갱신 중)', none: ' (렌더 전)' }[stateOf(frame)];
  $('frameNo').textContent = `${frame}  /  ${S.start}–${S.end}${st}`;
  drawBar();
}

const COLOR = { done: '#3a8f5a', stale: '#c08a2e', none: '#45464c' };
function drawBar() {
  if (!S) return;
  fit(bar);
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
  const x = (frame - S.start + 0.5) * fw;               // 현재 위치
  bctx.fillStyle = '#fff'; bctx.fillRect(x - r, 0, 2 * r, H);
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
}

let last = 0, acc = 0;
function tick(t) {
  if (playing && S) {
    acc += Math.min((t - last) / 1000, 0.25) * S.fps * +$('speed').value;
    while (acc >= 1) { acc -= 1; step(1, true); }
  } else acc = 0;
  last = t;
  renderThree();
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

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
$('setIn').onclick = () => { inF = frame; if (outF !== null && outF < inF) outF = null; drawBar(); };
$('setOut').onclick = () => { outF = frame; if (inF !== null && inF > outF) inF = null; drawBar(); };
$('clearRange').onclick = () => { inF = outF = null; drawBar(); };
$('follow').onchange = e => { if (e.target.checked && S) setFrame(S.frame, false); };
$('engine').onchange = $('res').onchange = () => send({ op: 'settings', engine: $('engine').value, res: +$('res').value });
$('auto').onchange = e => send({ op: 'settings', auto: e.target.checked });
$('rerender').onclick = () => send({ op: 'rerender' });
$('layout').onchange = e => { $('main').className = e.target.value; setTimeout(draw); };
addEventListener('resize', draw);

// 구간(없으면 전체)을 영상 파일로 저장. 브라우저 내장 MediaRecorder 사용
$('export').onclick = async () => {
  if (!S) return;
  const [a, b] = range();
  for (let f = a; f <= b; f++) {
    if (!imgs.get(f)) return say(`${f}프레임이 아직 렌더되지 않았습니다.`);
    if (checking.has(f)) return say('렌더가 끝난 뒤 저장해 주세요 (갱신 중인 프레임이 있음).');
  }
  const btn = $('export'); btn.disabled = true; btn.textContent = '저장 중…';
  const first = imgs.get(a), c = document.createElement('canvas');
  c.width = first.width; c.height = first.height;
  const x = c.getContext('2d');
  x.drawImage(first, 0, 0);
  const type = ['video/mp4;codecs=avc1', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm'].find(t => MediaRecorder.isTypeSupported(t));
  const stream = c.captureStream(0), track = stream.getVideoTracks()[0];
  const rec = new MediaRecorder(stream, { mimeType: type, videoBitsPerSecond: 8e6 });
  const chunks = [];
  rec.ondataavailable = e => chunks.push(e.data);
  const stopped = new Promise(r => (rec.onstop = r));
  rec.start();
  for (let f = a; f <= b; f++) {
    x.drawImage(imgs.get(f), 0, 0, c.width, c.height);
    track.requestFrame();
    await new Promise(r => setTimeout(r, 1000 / S.fps));
  }
  rec.stop(); await stopped;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob(chunks, { type }));
  link.download = `${S.file.replace(/\.blend$/, '')}_${a}-${b}.${type.startsWith('video/mp4') ? 'mp4' : 'webm'}`;
  link.click();
  btn.disabled = false; btn.textContent = '영상 저장';
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

let model = null, glbV = '', lastXf = null, camMode = false, holdCam = 0;
const byName = new Map();       // 블렌더 오브젝트 이름 → three 오브젝트

function loadGlb(v) {
  glbV = v;
  new GLTFLoader().load(`${API}/cache/scene.glb?v=${v}`, g => {
    if (v !== glbV) return;
    const sel = gizmo.object?.userData.bname;
    if (model) { scene.remove(model); model.traverse(o => { o.geometry?.dispose(); }); }
    model = g.scene;
    byName.clear();
    model.traverse(o => { if (o.userData.name) { o.userData.bname = o.userData.name; byName.set(o.userData.name, o); } });
    scene.add(model);
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
  lastXf = d;
  const dragging = gizmo.dragging ? gizmo.object : null;
  if (model) {
    const items = [];
    for (const [name, m] of Object.entries(d.obs)) {
      const o = byName.get(name);
      if (o && o !== dragging) items.push([o, C.clone().multiply(new THREE.Matrix4().set(...m)).multiply(Ci)]);
    }
    items.sort((a, b) => depth(a[0]) - depth(b[0]));   // 부모 먼저
    for (const [o, w] of items) setWorld(o, w);
  }
  camProxy.visible = !!d.cam;
  camHelper.visible = !!d.cam && !camMode;
  if (!d.cam) return;
  camProxy.userData.bname = d.cam.name;
  camProxy.userData.cam = d.cam;
  camProxy.aspect = d.aspect;
  if (document.activeElement !== $('lens')) { $('lens').value = d.cam.lens; $('lensVal').textContent = d.cam.lens.toFixed(0); }
  camProxy.fov = vfov(d.cam, d.aspect);
  camProxy.updateProjectionMatrix();
  if (dragging !== camProxy && performance.now() > holdCam) {
    setWorld(camProxy, C.clone().multiply(new THREE.Matrix4().set(...d.obs[d.cam.name])));
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

gizmo.addEventListener('dragging-changed', e => {
  orbit.enabled = !e.value;
  if (!e.value) pushXf(gizmo.object, true);
});
gizmo.addEventListener('objectChange', () => { pushXf(gizmo.object); if (gizmo.object === camProxy) camHelper.update(); });

function select(o) {
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
  camMode = on;
  $('camView').classList.toggle('on', on);
  camPick.visible = !on; camHelper.visible = !on && camProxy.visible;
  if (gizmo.object === camProxy) select(null);
  orbit.object = gizmo.camera = on ? camProxy : freeCam;
  // 켤 때 update() 를 부르면 카메라가 target 을 향해 다시 돌면서 블렌더 카메라가 움직이므로 끌 때만
  if (on) syncOrbitTarget(); else { orbit.target.set(0, 0, 0); orbit.update(); }
}
$('camView').onclick = () => setCamMode(!camMode);
// 조작 중에는 블렌더에서 오는 카메라 값을 잠시 무시 (end 이벤트가 빠져도 0.5초 뒤 풀림)
orbit.addEventListener('change', () => {
  if (!camMode) return;
  camProxy.rotateZ(camRoll);
  holdCam = performance.now() + 500;
  pushXf(camProxy);
});
orbit.addEventListener('end', () => { if (camMode) { pushXf(camProxy, true); holdCam = performance.now() + 500; } });

$('lens').oninput = e => {
  const c = camProxy.userData.cam; if (!c) return;
  c.lens = +e.target.value; $('lensVal').textContent = c.lens;
  camProxy.fov = vfov(c, camProxy.aspect); camProxy.updateProjectionMatrix(); camHelper.update();
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
addEventListener('keydown', e => {
  if (e.target.matches('input, select') || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === ' ') { e.preventDefault(); setPlaying(!playing); }
  else if (k === 'arrowleft') step(-1);
  else if (k === 'arrowright') step(1);
  else if (k === 'i') $('setIn').click();
  else if (k === 'o') $('setOut').click();
  else if (k === 'g') setMode('translate');
  else if (k === 'r') setMode('rotate');
  else if (k === 's') setMode('scale');
  else if (k === 'c') setCamMode(!camMode);
  else if (k === 'escape') select(null);
});
