bl_info = {
    "name": "Live Preview (실시간 미리보기)",
    "author": "",
    "version": (0, 1, 0),
    "blender": (4, 1, 0),
    "location": "3D 뷰 > 사이드바(N) > 미리보기",
    "description": "편집하면 자동 렌더, 브라우저에서 영상 재생과 구도 조절",
    "category": "Render",
}

import bpy
import os
import json
import time
import queue
import hashlib
import threading
import subprocess
import tempfile
import io
import zipfile
import webbrowser
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from mathutils import Matrix

REPO = "kji1284557-hub/blender-live-preview"  # 자동 업데이트를 받는 GitHub 저장소 (릴리스의 zip)

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
WORKER = os.path.join(HERE, "worker.py")
ROOT = os.path.join(tempfile.gettempdir(), "blender_live_preview")
DEBOUNCE = 1.5  # 편집이 멈추고 이만큼 지나면 렌더 시작(초)
ENGINES = ("WORKBENCH", "EEVEE", "CYCLES")
RESOLUTIONS = (25, 50, 75, 100)
# 사이트는 애드온이 직접 띄우므로(버전이 항상 애드온과 같음) 허용 출처는 자기 자신뿐
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".jpg": "image/jpeg", ".glb": "model/gltf-binary", ".json": "application/json"}

# 편집 감지 분류: 무시 / 변환만(프레임 해시 비교로 충분) / 그 외 데이터는 전체 다시 렌더
IGNORE = (bpy.types.Screen, bpy.types.WorkSpace, bpy.types.WindowManager,
          bpy.types.Brush, bpy.types.Palette, bpy.types.Text)
LIGHT = (bpy.types.Action, bpy.types.Collection, bpy.types.Key)
XF_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT", "EMPTY", "CAMERA", "LIGHT", "ARMATURE"}


class St:
    """애드온 상태. bpy 접근은 메인 스레드에서만, HTTP 스레드는 snap/xf 문자열과 cmds 큐만 만진다."""
    server = None
    worker = None
    out = queue.Queue()     # 워커 → 메인
    cmds = queue.Queue()    # 브라우저 → 메인
    clients = set()         # SSE 클라이언트 큐
    dir = ""
    frames = {}             # 프레임 → 렌더 버전
    checking = set()        # 현재 작업에서 아직 확인 안 된 프레임
    busy = False
    glb = ""
    engine = "WORKBENCH"
    res = 50
    auto = True
    dirty = 0.0             # 마지막 편집 시각, 0 = 깨끗
    full = False
    geo = False
    sigs = {}
    sigs_stale = False
    scene_sig = None
    last_frame = None
    sent_frame = None
    rng = None
    xf_dirty = True
    job = 0
    error = ""
    snap = "{}"
    xf = "{}"
    update = None           # (새 버전, zip 바이트) — 백그라운드 확인 스레드가 채우고 tick 이 적용


def scene():
    return bpy.context.scene or bpy.context.window_manager.windows[0].scene


def window_ctx():
    return bpy.context.temp_override(window=bpy.context.window_manager.windows[0])


def is_playing():
    return any(w.screen and w.screen.is_animation_playing for w in bpy.context.window_manager.windows)


def mat_list(m):
    return [round(v, 5) for row in m for v in row]


def scene_sig(sc):
    """렌더에 영향을 주는 장면 설정. Scene 업데이트는 선택 변경에도 오므로 값으로 비교한다."""
    r, v = sc.render, sc.view_settings

    def excluded(lc):
        yield lc.name, lc.exclude
        for c in lc.children:
            yield from excluded(c)

    return (sc.camera and sc.camera.name, sc.world and sc.world.name, r.resolution_x, r.resolution_y,
            r.pixel_aspect_x, r.pixel_aspect_y, r.film_transparent, v.view_transform, v.look,
            round(v.exposure, 4), round(v.gamma, 4), getattr(getattr(sc, "cycles", None), "samples", None),
            sc.eevee.taa_render_samples, tuple(x for vl in sc.view_layers for x in excluded(vl.layer_collection)))


def animated(idb):
    """재생 중에 오는 업데이트가 애니메이션 때문인지 (그렇다면 편집이 아님)"""
    for owner in (idb, getattr(idb, "node_tree", None)):
        ad = getattr(owner, "animation_data", None)
        if ad and (ad.action or len(ad.drivers)):
            return True
    return False


def obj_sig(ob):
    s = (tuple(mat_list(ob.matrix_world)), ob.hide_render)
    if ob.pose:
        s += tuple(v for pb in ob.pose.bones for row in pb.matrix_basis for v in row)
    return s


# ---------------- 브라우저로 보내기 ----------------

def broadcast(kind, payload):
    msg = f"event: {kind}\ndata: {payload}\n\n"
    for q in list(St.clients):
        q.put(msg)


def push_state(send=True):
    sc = scene()
    r = sc.render
    St.snap = json.dumps(dict(
        file=os.path.basename(bpy.data.filepath) or "(저장 안 된 파일)",
        start=sc.frame_start, end=sc.frame_end, fps=r.fps / r.fps_base, frame=sc.frame_current,
        markers=sorted([m.frame, m.name] for m in sc.timeline_markers),
        frames=St.frames, checking=sorted(St.checking), busy=St.busy, glb=St.glb,
        engine=St.engine, res=St.res, auto=St.auto, error=St.error,
        version=".".join(map(str, bl_info["version"])),
    ))
    if send:
        broadcast("state", St.snap)


def push_xf():
    sc = scene()
    obs = {ob.name: mat_list(ob.matrix_world) for ob in sc.objects if ob.type in XF_TYPES}
    cam = None
    if sc.camera and sc.camera.type == "CAMERA":
        d = sc.camera.data
        cam = dict(name=sc.camera.name, lens=d.lens, sensor=d.sensor_width, sensor_h=d.sensor_height,
                   fit=d.sensor_fit)
    r = sc.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / (r.resolution_y * r.pixel_aspect_y)
    St.xf = json.dumps(dict(obs=obs, cam=cam, aspect=aspect))
    broadcast("xf", St.xf)


# ---------------- 편집 감지 ----------------

def mark(full=False, geo=False):
    St.full |= full
    St.geo |= geo
    St.dirty = time.time()


@bpy.app.handlers.persistent
def on_depsgraph(sc, depsgraph):
    St.xf_dirty = True
    moving = sc.frame_current != St.last_frame or is_playing()
    if moving:
        on_frame(sc)  # 프레임 이동·재생 때문에 바뀐 변환·애니메이션 값은 편집이 아님
    changed = full = pose = False
    geo_only = False
    for u in depsgraph.updates:
        idb = u.id.original
        if isinstance(idb, bpy.types.Scene):
            s = scene_sig(idb)
            if s != St.scene_sig:
                St.scene_sig = s
                if not moving:  # 재생 중 마커로 카메라가 바뀌는 것도 애니메이션
                    changed = full = True
        elif moving and (isinstance(idb, bpy.types.Object) or animated(idb)):
            pass
        elif isinstance(idb, bpy.types.Object):
            s = obj_sig(idb)
            if St.sigs.get(idb.name) != s:
                St.sigs[idb.name] = s
                changed = True
                pose |= idb.type == "ARMATURE"
            elif u.is_updated_geometry:
                geo_only = True  # 모디파이어 수정 등, 변환은 그대로인데 형태가 바뀜
        elif isinstance(idb, IGNORE):
            pass
        elif isinstance(idb, LIGHT):
            changed = True
        else:
            changed = full = True  # 메시·재질·라이트·월드 등 데이터 수정
    if geo_only and not pose:
        changed = full = True
    if changed:
        mark(full, geo=full or pose)


@bpy.app.handlers.persistent
def on_frame(sc, *_):
    # 프레임 이동은 depsgraph_update_post 를 부르지 않으므로 여기서 기록.
    # 애니메이션 때문에 바뀐 sig 는 편집이 아니니 tick 에서 기준을 다시 잡는다
    St.xf_dirty = True
    St.last_frame = sc.frame_current
    St.sigs_stale = True


def refresh_sigs():
    St.sigs = {ob.name: obj_sig(ob) for ob in scene().objects}
    St.sigs_stale = False


@bpy.app.handlers.persistent
def on_load(*_):
    St.dir = ""  # 다음 tick 에서 새 파일 기준으로 초기화


def reset_for_file():
    key = bpy.data.filepath or "untitled"
    St.dir = os.path.join(ROOT, hashlib.md5(key.encode("utf-8")).hexdigest()[:12])
    os.makedirs(os.path.join(St.dir, "frames"), exist_ok=True)
    hp = os.path.join(St.dir, "hashes.json")
    St.frames, St.checking, St.glb, St.error = {}, set(), "", ""
    St.full = False
    try:
        with open(hp, encoding="utf-8") as f:
            h = json.load(f)
        St.frames = {int(k): v[:8] for k, v in h.items() if not k.startswith("_")}
        # 지난번 렌더 이후 파일이 저장됐으면 형태가 바뀌었을 수 있으니 전체 렌더
        St.full = bool(bpy.data.filepath) and os.path.getmtime(bpy.data.filepath) > os.path.getmtime(hp)
    except (OSError, ValueError):
        pass
    if os.path.exists(os.path.join(St.dir, "scene.glb")):
        St.glb = str(os.path.getmtime(os.path.join(St.dir, "scene.glb")))
    sc = scene()
    refresh_sigs()
    St.scene_sig = scene_sig(sc)
    St.last_frame = sc.frame_current
    St.geo = True
    St.dirty = time.time() - DEBOUNCE  # 바로 첫 작업
    St.xf_dirty = True
    push_state()


# ---------------- 렌더 워커 ----------------

def start_worker():
    flags = 0
    if os.name == "nt":
        flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    p = subprocess.Popen([bpy.app.binary_path, "-b", "--python", WORKER],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", creationflags=flags)
    threading.Thread(target=read_worker, args=(p,), daemon=True).start()
    St.worker = p


def read_worker(p):
    for line in p.stdout:
        if line.startswith("@@"):
            St.out.put(json.loads(line[2:]))
    St.out.put({"t": "exit", "p": p})


def submit():
    sc = scene()
    copy = os.path.join(St.dir, "scene.blend")
    with window_ctx():
        bpy.ops.wm.save_as_mainfile(filepath=copy, copy=True, relative_remap=True,
                                    compress=False, check_existing=False)
    St.job += 1
    job = dict(id=St.job, blend=copy, dir=St.dir, frame=sc.frame_current, full=St.full, geo=St.geo,
               engine=St.engine, res=St.res)
    St.full = St.geo = False
    St.dirty = 0.0
    line = json.dumps(job) + "\n"
    for _ in range(2):  # 워커가 죽어 있으면 한 번 다시 띄워서 보낸다
        if St.worker is None or St.worker.poll() is not None:
            start_worker()
        try:
            St.worker.stdin.write(line)
            St.worker.stdin.flush()
            break
        except OSError:
            St.worker = None
    St.busy = True
    St.error = ""
    St.checking = set(range(sc.frame_start, sc.frame_end + 1))
    push_state()


def on_worker(m):
    t = m["t"]
    if t == "exit":
        if m["p"] is not St.worker:  # 이미 새 워커로 바뀐 뒤 들어온 옛 워커의 종료
            return
    elif m.get("dir") != St.dir or (t == "idle" and m.get("id") != St.job):
        return  # 파일을 바꾸기 전 작업, 또는 새 작업이 이미 나간 뒤의 idle
    if t in ("done", "ok"):
        f = m["f"]
        if t == "done":
            St.frames[f] = m["v"]
        St.checking.discard(f)
        broadcast("frame", json.dumps(dict(f=f, v=St.frames.get(f))))
        push_state(send=False)  # 재접속한 브라우저가 최신 상태를 받게
    elif t == "glb":
        St.glb = m["v"]
        broadcast("glb", json.dumps(dict(v=St.glb)))
    elif t == "idle":
        St.busy = False
        St.checking = set()
        push_state()
    elif t == "error":
        St.error = m["msg"]
        push_state()
    elif t == "exit":
        St.worker = None
        if St.busy:
            St.busy = False
            St.error = "렌더 프로세스가 종료됐습니다 (다음 편집 때 다시 시작)"
        push_state()


# ---------------- 브라우저 명령 ----------------

def keyframe(ob):
    rot = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(ob.rotation_mode, "rotation_euler")
    for path in ("location", rot, "scale"):
        ob.keyframe_insert(path)


def run_cmd(c):
    op = c.get("op")
    sc = scene()
    if op == "frame":
        sc.frame_set(int(c["f"]))
    elif op == "xform":
        ob = bpy.data.objects.get(str(c.get("name")))
        m = c.get("m")
        if ob is None or not isinstance(m, list) or len(m) != 16:
            return
        ob.matrix_world = Matrix([[float(v) for v in m[i * 4:i * 4 + 4]] for i in range(4)])
        if c.get("end"):
            if c.get("key"):
                keyframe(ob)
            with window_ctx():
                bpy.ops.ed.undo_push(message="미리보기 구도 조절")
    elif op == "lens":
        ob = bpy.data.objects.get(str(c.get("name")))
        if ob is None or ob.type != "CAMERA":
            return
        ob.data.lens = min(max(float(c["lens"]), 1.0), 5000.0)
        if c.get("end"):
            if c.get("key"):
                ob.data.keyframe_insert("lens")
            with window_ctx():
                bpy.ops.ed.undo_push(message="미리보기 렌즈 조절")
    elif op == "settings":
        engine, res = c.get("engine", St.engine), int(c.get("res", St.res))
        if engine in ENGINES and res in RESOLUTIONS and (engine, res) != (St.engine, St.res):
            St.engine, St.res = engine, res
            mark()  # 워커가 설정 변경을 보고 전체 렌더
        if "auto" in c:
            St.auto = bool(c["auto"])
        push_state()
    elif op == "rerender":
        mark(full=True)


# ---------------- 메인 루프 (타이머) ----------------

def tick():
    try:
        if St.update:
            bpy.app.timers.register(apply_update)  # 이 tick 바깥에서 애드온을 껐다 켠다
            return None
        if not St.dir:
            reset_for_file()
        while not St.cmds.empty():
            run_cmd(St.cmds.get())
        while not St.out.empty():
            on_worker(St.out.get())
        sc = scene()
        if St.sigs_stale and not is_playing():
            refresh_sigs()
        rng = (sc.frame_start, sc.frame_end)
        if rng != St.rng:
            if St.rng is not None:
                mark()  # 범위가 늘면 새 프레임 렌더
            St.rng = rng
            push_state()
        if sc.frame_current != St.sent_frame:
            St.sent_frame = sc.frame_current
            broadcast("cur", json.dumps(dict(f=sc.frame_current)))
        if St.xf_dirty:
            St.xf_dirty = False
            push_xf()
        if St.auto and St.dirty and time.time() - St.dirty > DEBOUNCE and not is_playing():
            submit()
    except Exception as e:  # 타이머가 죽으면 전체가 멈추므로 기록만 하고 계속
        St.error = f"{type(e).__name__}: {e}"
        print("[live_preview]", St.error)
    return 0.1


# ---------------- HTTP 서버 ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        o = self.headers.get("Origin")
        if o in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", cache="no-cache"):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, base, rel, cache="no-cache"):
        base = os.path.realpath(base)
        path = os.path.realpath(os.path.join(base, rel))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return self._send(404, b"not found")
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(path)[1], "application/octet-stream"), cache)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:  # DNS 리바인딩 차단
            return self._send(403, b"forbidden")
        p = urlparse(self.path).path
        if p == "/events":
            return self._events()
        if p == "/api/state":
            return self._send(200, St.snap.encode(), "application/json")
        if p.startswith("/cache/") and St.dir:
            # 프레임은 ?v=버전 으로 주소가 바뀌므로 캐시해도 안전
            return self._file(St.dir, p[len("/cache/"):], "max-age=31536000")
        return self._file(WEB, p.lstrip("/") or "index.html")

    def do_POST(self):
        o = self.headers.get("Origin")
        if self.headers.get("Host") not in HOSTS or (o is not None and o not in ORIGINS):
            return self._send(403, b"forbidden")
        if urlparse(self.path).path != "/api/cmd" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(400, b"bad request")
        try:
            c = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self._send(400, b"bad json")
        if not isinstance(c, dict):
            return self._send(400, b"bad json")
        St.cmds.put(c)
        self._send(204)

    def _events(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        St.clients.add(q)  # 스냅샷보다 먼저 등록해야 그 사이 이벤트를 놓치지 않는다
        q.put(f"event: state\ndata: {St.snap}\n\n")
        q.put(f"event: xf\ndata: {St.xf}\n\n")
        if St.sent_frame is not None:
            q.put(f"event: cur\ndata: {json.dumps(dict(f=St.sent_frame))}\n\n")
        try:
            while St.server is not None:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": ping\n\n"
                if msg is None:
                    break
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            St.clients.discard(q)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # 윈도우에서 다른 블렌더와 포트를 같이 잡지 않게
    daemon_threads = True


def start_server():
    try:
        St.server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        St.error = f"포트 {PORT} 사용 중: 다른 블렌더가 이미 미리보기를 켜두었습니다"
        print("[live_preview]", St.error)
        return
    threading.Thread(target=St.server.serve_forever, daemon=True).start()


def stop_server():
    srv, St.server = St.server, None
    for q in list(St.clients):
        q.put(None)
    if srv:
        srv.shutdown()
        srv.server_close()


# ---------------- 자동 업데이트 ----------------

def check_update():
    """백그라운드 스레드. GitHub 최신 릴리스가 더 새 버전이면 zip 을 받아 둔다 (적용은 메인 스레드)."""
    try:
        get = lambda url: urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "blender-live-preview"}), timeout=30)
        rel = json.load(get(f"https://api.github.com/repos/{REPO}/releases/latest"))
        latest = tuple(int(x) for x in rel["tag_name"].lstrip("v").split("."))
        if latest <= tuple(bl_info["version"]):
            return
        url = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith(".zip"))
        St.update = (latest, get(url).read())
    except Exception as e:  # 오프라인 등: 지금 버전으로 계속
        print("[live_preview] 업데이트 확인 실패:", e)


def apply_update():
    """애드온을 끄고 → 파일 교체 → 다시 켠다. 다시 켤 때 블렌더가 바뀐 모듈을 새로 읽는다."""
    import addon_utils
    latest, data = St.update
    St.update = None
    name = __name__
    addon_utils.disable(name, default_set=False)
    try:
        root, folder = os.path.dirname(HERE), os.path.basename(HERE)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                top, _, rest = n.partition("/")
                if top != "live_preview" or not rest or n.endswith("/") or ".." in rest.split("/"):
                    continue
                dst = os.path.join(root, folder, *rest.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
        print("[live_preview] 업데이트 완료:", ".".join(map(str, latest)))
    finally:
        addon_utils.enable(name, default_set=False)
    return None


# ---------------- UI ----------------

class LP_OT_open(bpy.types.Operator):
    bl_idname = "live_preview.open"
    bl_label = "미리보기 사이트 열기"

    def execute(self, ctx):
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return {"FINISHED"}


class LP_OT_rerender(bpy.types.Operator):
    bl_idname = "live_preview.rerender"
    bl_label = "전체 다시 렌더"

    def execute(self, ctx):
        mark(full=True)
        return {"FINISHED"}


class LP_OT_auto(bpy.types.Operator):
    bl_idname = "live_preview.toggle_auto"
    bl_label = "자동 렌더 켜기/끄기"

    def execute(self, ctx):
        St.auto = not St.auto
        push_state()
        return {"FINISHED"}


class LP_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "미리보기"
    bl_label = "실시간 미리보기"

    def draw(self, ctx):
        l = self.layout
        l.operator("live_preview.open", icon="URL")
        l.label(text="버전 " + ".".join(map(str, bl_info["version"])))
        l.label(text=f"서버: {'켜짐' if St.server else '꺼짐'}  (포트 {PORT})")
        l.label(text=f"렌더: {'진행 중' if St.busy else '대기'}  {St.engine} {St.res}%")
        l.operator("live_preview.toggle_auto", text=f"자동 렌더: {'켜짐' if St.auto else '꺼짐'}")
        l.operator("live_preview.rerender", icon="FILE_REFRESH")
        if St.error:
            l.label(text=St.error, icon="ERROR")


classes = (LP_OT_open, LP_OT_rerender, LP_OT_auto, LP_PT_panel)
HANDLERS = ((bpy.app.handlers.depsgraph_update_post, on_depsgraph),
            (bpy.app.handlers.frame_change_post, on_frame),
            (bpy.app.handlers.load_post, on_load))


def register():
    for c in classes:
        bpy.utils.register_class(c)
    if bpy.app.background:  # 렌더 워커 안에서 애드온이 로드될 때는 서버를 띄우지 않는다
        return
    start_server()
    if St.server is None:  # 다른 블렌더가 이미 쓰는 중: 같은 캐시를 두 워커가 덮어쓰지 않게 아무것도 안 함
        return
    for lst, fn in HANDLERS:
        lst.append(fn)
    St.dir = ""
    bpy.app.timers.register(tick, first_interval=0.5, persistent=True)
    threading.Thread(target=check_update, daemon=True).start()


def unregister():
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for lst, fn in HANDLERS:
        if fn in lst:
            lst.remove(fn)
    stop_server()
    if St.worker and St.worker.poll() is None:
        St.worker.kill()
    St.worker = None
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
