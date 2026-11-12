bl_info = {
    "name": "Live Preview (실시간 미리보기)",
    "author": "",
    "version": (0, 1, 2),
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
import shutil
import hashlib
import threading
import subprocess
import tempfile
import io
import zipfile
import webbrowser
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from mathutils import Matrix

REPO = "kji1284557-hub/blender-live-preview"  # 자동 업데이트를 받는 GitHub 저장소 (릴리스의 zip)

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
WORKER = os.path.join(HERE, "worker.py")
ROOT = os.path.join(tempfile.gettempdir(), "blender_live_preview")
DEBOUNCE = 1.5      # 편집이 멈추고 이만큼 지나면 렌더 시작(초)
STUCK = 5.0         # 새 편집이 왔는데 워커가 이만큼 소식이 없으면(긴 프레임 렌더 중) 끊고 새로 시작
CACHE_DAYS = 14     # 이보다 오래 안 쓴 파일별 캐시는 지운다
ENGINES = ("WORKBENCH", "EEVEE", "CYCLES")
RESOLUTIONS = (25, 50, 75, 100)
# 사이트는 애드온이 직접 띄우므로(버전이 항상 애드온과 같음) 허용 출처는 자기 자신뿐
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
        ".jpg": "image/jpeg", ".glb": "model/gltf-binary", ".json": "application/json",
        ".wasm": "application/wasm"}

# 편집 감지 분류: 무시 / 프레임 해시 비교로 충분(바뀐 프레임만) / 그 외 데이터는 전체 다시 렌더
IGNORE = (bpy.types.Screen, bpy.types.WorkSpace, bpy.types.WindowManager,
          bpy.types.Brush, bpy.types.Palette, bpy.types.Text)
# 라이트는 크기·모양·노드 등 값이 많아 전체 렌더 쪽에 둔다. 카메라는 워커가 모든 속성을 해시로 비교.
# 아마추어(포즈를 바꾸면 함께 업데이트됨)는 본 행렬·변형된 정점을 해시가 비교한다
LIGHT = (bpy.types.Action, bpy.types.Collection, bpy.types.Key, bpy.types.Camera, bpy.types.Armature)
# 미리보기 렌더 결과와 무관한 장면 설정 (워커가 덮어쓰거나, 화면·성능에만 쓰임)
SKIP_RNA = {"rna_type", "name", "filepath", "engine", "use_lock_interface", "fps", "fps_base",
            "resolution_percentage", "threads", "threads_mode", "use_persistent_data", "preview_pixel_size",
            "use_file_extension", "use_overwrite", "use_placeholder", "use_sequencer", "use_compositing",
            "device", "feature_set", "shading_system", "debug_use_spatial_splits"}
# 카메라·라이트 데이터를 바꾸면 그 오브젝트에도 '형태 변경' 표시가 오지만, 렌더 형태와는 무관 (해시가 값을 비교)
NO_GEOMETRY = {"CAMERA", "LIGHT", "EMPTY", "ARMATURE", "SPEAKER", "LIGHT_PROBE", "LIGHTPROBE"}
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
    checking = set()        # 아직 최신인지 확인 안 된 프레임 (주황)
    busy = False
    job_err = False         # 지금 작업이 오류로 끝났으면 남은 프레임을 '최신'으로 바꾸지 않는다
    pending = (False, False)  # 보냈지만 아직 워커가 시작했다고 확인 안 된 (전체 다시, 3D 다시)
    heard = False           # 지금 워커에게서 소식을 한 번이라도 받았는지
    last_msg = 0.0          # 워커에게서 마지막으로 소식을 받은 시각
    glb = ""
    engine = "WORKBENCH"
    res = 50
    auto = True
    force = False           # 자동 렌더가 꺼져 있어도 이번 한 번은 렌더 (버튼·설정 변경)
    dirty = 0.0             # 마지막 편집 시각, 0 = 깨끗
    full = False
    geo = False
    last_edit = 0.0         # 마지막 실제 편집 시각
    saved_at = 0.0          # 작업자가 마지막으로 저장한 시각
    save_mtime = None       # 그 저장 파일의 수정 시각 (렌더가 따라잡으면 saved.json 에 기록)
    copying = False         # 렌더용 복사본을 저장하는 중
    play_edit = False       # 재생 중에 사람이 옮긴 것으로 보이는 변경이 있었음
    sigs = {}
    sigs_stale = False
    scene_sig = None
    last_frame = None
    sent_frame = None
    rng = None
    meta = None
    playing = False
    xf_dirty = True
    snap_dirty = False
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


def in_modal():
    """G 이동·스컬프트 같은 조작 도중인지 (4.2+ 만 알 수 있음)"""
    return any(len(getattr(w, "modal_operators", ())) for w in bpy.context.window_manager.windows)


def mat_list(m):
    return [round(v, 5) for row in m for v in row]


def rna_vals(s):
    """구조체의 단순 값(숫자·불·열거·문자열)을 모두 모은다. 설정 항목을 하나하나 나열하면 빠뜨리기 쉬워서."""
    if s is None:
        return ()
    out = []
    for p in s.bl_rna.properties:
        i = p.identifier
        if i in SKIP_RNA or i.startswith(("preview_", "viewport", "use_preview")) or p.type in {"POINTER", "COLLECTION"}:
            continue
        v = getattr(s, p.identifier, None)
        out.append(tuple(v) if getattr(p, "is_array", False) else v)
    return tuple(out)


def scene_sig(sc):
    """렌더에 영향을 주는 장면 설정. Scene 업데이트는 선택 변경에도 오므로 값으로 비교한다.
    첫 칸은 활성 카메라(재생 중 마커로 바뀌는 건 애니메이션이라 따로 본다)."""
    def excluded(lc):
        yield lc.name, lc.exclude
        for c in lc.children:
            yield from excluded(c)

    return (sc.camera and sc.camera.name, sc.world and sc.world.name,
            rna_vals(sc.render), rna_vals(sc.view_settings), rna_vals(sc.display.shading), rna_vals(sc.eevee),
            rna_vals(getattr(sc, "cycles", None)),
            tuple(x for vl in sc.view_layers for x in excluded(vl.layer_collection)))


def animated(idb):
    """재생 중에 오는 업데이트가 애니메이션 때문인지 (그렇다면 편집이 아님). NLA 스트립만 있는 경우 포함"""
    for owner in (idb, getattr(idb, "node_tree", None)):
        ad = getattr(owner, "animation_data", None)
        if ad and (ad.action or len(ad.drivers) or any(not t.mute for t in ad.nla_tracks)):
            return True
    return False


def moved_by_parent(ob):
    """부모 사슬 어딘가가 애니메이션·컨스트레인트로 움직이면, 이 오브젝트가 움직여도 사람이 옮긴 게 아님"""
    p = ob.parent
    while p is not None:
        if animated(p) or len(p.constraints):
            return True
        p = p.parent
    return False


def obj_sig(ob):
    s = (tuple(mat_list(ob.matrix_world)), ob.hide_render, ob.instance_type,
         ob.instance_collection and ob.instance_collection.name)
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
    St.snap_dirty = False
    St.snap = json.dumps(dict(
        file=os.path.basename(bpy.data.filepath) or "(저장 안 된 파일)", dir=os.path.basename(St.dir),
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
        # 행렬을 따로 보낸다: 활성 카메라가 이 씬에 링크되지 않은 오브젝트일 수도 있어서 obs 에 없을 수 있음
        cam = dict(name=sc.camera.name, lens=d.lens, sensor=d.sensor_width, sensor_h=d.sensor_height,
                   fit=d.sensor_fit, m=mat_list(sc.camera.matrix_world))
    r = sc.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / (r.resolution_y * r.pixel_aspect_y)
    St.xf = json.dumps(dict(obs=obs, cam=cam, aspect=aspect))
    broadcast("xf", St.xf)


# ---------------- 편집 감지 ----------------

def mark(full=False, geo=False, edit=True):
    """렌더 작업 예약. edit=False 는 파일 내용은 그대로인 경우(엔진 변경·다시 렌더 버튼)."""
    St.full |= full
    St.geo |= geo
    St.dirty = time.time()
    if edit and St.dir:  # 파일을 막 열어 아직 폴더가 정해지기 전이면 지울 것도 없음 (빈 경로 = 현재 폴더)
        St.last_edit = St.dirty
        try:
            os.remove(os.path.join(St.dir, "saved.json"))  # 렌더가 저장된 파일과 달라지기 시작함
        except OSError:
            pass


def trust_saved():
    """마지막 저장 이후 편집이 없고 렌더도 끝났으면, 다음에 열 때 캐시를 그대로 써도 된다고 기록."""
    if (St.save_mtime and St.dir and not St.busy and not St.dirty and not St.full
            and St.last_edit < St.saved_at):
        with open(os.path.join(St.dir, "saved.json"), "w", encoding="utf-8") as f:
            json.dump({"mtime": St.save_mtime}, f)
        St.save_mtime = None


@bpy.app.handlers.persistent
def on_depsgraph(sc, depsgraph):
    St.xf_dirty = True
    moving = sc.frame_current != St.last_frame or is_playing()
    if moving:
        on_frame(sc)  # 프레임 이동·재생 때문에 바뀐 변환·애니메이션 값은 편집이 아님
    changed = full = pose = geo = False
    geo_only = False
    for u in depsgraph.updates:
        idb = u.id.original
        if isinstance(idb, bpy.types.Scene):
            s = scene_sig(idb)
            if s != St.scene_sig:
                # 재생 중 마커로 카메라만 바뀌는 건 애니메이션, 그 외 설정이 바뀌었으면 편집
                if not moving or St.scene_sig is None or s[1:] != St.scene_sig[1:]:
                    changed = full = True
                St.scene_sig = s
        elif moving and isinstance(idb, bpy.types.Object):
            # 재생 중에도 애니메이션 없는 오브젝트가 움직였다면 사람이 옮긴 것 → 멈추면 확인
            if (u.is_updated_transform and is_playing() and not animated(idb) and not moved_by_parent(idb)
                    and not len(idb.constraints)):
                St.play_edit = True
        elif moving and (animated(idb) or isinstance(idb, bpy.types.Action)):
            pass  # 4.1 은 재생 중 매 프레임 Action 도 업데이트로 보낸다 (편집 아님)
        elif isinstance(idb, bpy.types.Object):
            s = obj_sig(idb)
            old = St.sigs.get(idb.name)
            if old != s:
                St.sigs[idb.name] = s
                changed = True
                pose |= idb.type == "ARMATURE"
                geo |= old is None or old[1] != s[1]  # 새 오브젝트·이름 변경·렌더 숨김은 3D 화면도 다시
            elif u.is_updated_geometry and idb.type not in NO_GEOMETRY:
                geo_only = True  # 모디파이어 수정 등, 변환은 그대로인데 형태가 바뀜
        elif getattr(idb, "bl_idname", "") == "CompositorNodeTree":
            changed = full = True  # 컴포지터(4.x 는 씬에 딸린 데이터)
        elif isinstance(idb, IGNORE) or (getattr(idb, "is_embedded_data", False)
                                          and not isinstance(idb, bpy.types.Collection)):
            pass  # 재질·라이트·월드에 딸린 노드 트리: 함께 오는 주인 데이터의 분류를 따른다
            # (씬의 최상위 컬렉션도 내장 데이터지만, 오브젝트 추가·삭제가 여기로 오므로 아래 컬렉션 규칙을 따른다)
        elif isinstance(idb, LIGHT):
            changed = True
            geo |= isinstance(idb, bpy.types.Collection)  # 오브젝트 삭제·컬렉션 숨김은 3D 화면도 다시
        else:
            changed = full = True  # 메시·재질·월드 등 데이터 수정
    if geo_only and not pose:
        changed = full = True
    if changed:
        mark(full, geo=full or pose or geo)


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


def cache_dir():
    return os.path.join(ROOT, hashlib.md5((bpy.data.filepath or "untitled").encode("utf-8")).hexdigest()[:12])


@bpy.app.handlers.persistent
def on_load(*_):
    St.dir = ""  # 다음 tick 에서 새 파일 기준으로 초기화
    St.save_mtime = None  # 이전 파일의 저장 기록이 새 파일로 넘어가지 않게


@bpy.app.handlers.persistent
def on_save(path=None, *_):
    # 렌더용 복사본, 또는 작업자의 '복사본 저장'(원본 파일은 그대로)은 저장으로 치지 않는다
    if St.copying or os.path.normcase(path or "") != os.path.normcase(bpy.data.filepath):
        return
    # 저장 시점을 기억해 두고, 렌더가 끝나는 대로(지금 끝나 있으면 바로) 캐시를 믿을 수 있다고 기록
    St.saved_at = time.time()
    St.save_mtime = os.path.getmtime(bpy.data.filepath)
    if cache_dir() != St.dir:  # 다른 이름으로 저장: 새 파일(새 캐시 폴더)로 다시 시작, 렌더가 끝나면 기록
        St.dir = ""
        return
    trust_saved()


def reset_for_file():
    St.dir = cache_dir()
    os.makedirs(os.path.join(St.dir, "frames"), exist_ok=True)
    os.utime(St.dir)  # 오래된 캐시 정리 기준
    St.frames, St.checking, St.glb, St.error = {}, set(), "", ""
    St.busy = St.job_err = False
    trusted = False
    try:
        with open(os.path.join(St.dir, "hashes.json"), encoding="utf-8") as f:
            h = json.load(f)
        St.frames = {int(k): v[:8] for k, v in h.items() if not k.startswith("_")}
        engine, res = h.get("_settings", "").split("/")[:2]
        if engine in ENGINES and int(res) in RESOLUTIONS:  # 지난번 렌더 설정을 이어서 (캐시 재사용)
            St.engine, St.res = engine, int(res)
        with open(os.path.join(St.dir, "saved.json"), encoding="utf-8") as f:
            trusted = bool(bpy.data.filepath) and json.load(f)["mtime"] == os.path.getmtime(bpy.data.filepath)
    except (OSError, ValueError, KeyError):
        pass
    # 저장 안 한 편집을 버리고 다시 연 경우 등: 캐시가 지금 파일과 같다고 확신할 수 없으면 전체 렌더
    St.full = not trusted
    glb = os.path.join(St.dir, "scene.glb")
    if os.path.exists(glb):
        St.glb = str(os.path.getmtime(glb))
    sc = scene()
    refresh_sigs()
    St.scene_sig = scene_sig(sc)
    St.last_frame = sc.frame_current
    St.geo = True
    St.dirty = time.time() - DEBOUNCE  # 바로 첫 작업
    St.force = True
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
    St.heard = False  # 막 띄운 워커는 파일 열기 등으로 첫 소식이 늦을 수 있다 → 멈춤 판정에서 뺀다


def read_worker(p):
    for line in p.stdout:
        if line.startswith("@@"):
            try:
                St.out.put(json.loads(line[2:]))
            except ValueError:
                pass  # 깨진 줄 하나 때문에 읽기를 멈추면 파이프가 막혀 워커가 멈춘다
    St.out.put({"t": "exit", "p": p})


def kill_worker():
    if St.worker and St.worker.poll() is None:
        St.worker.kill()
    St.worker = None
    restore_pending()


def restore_pending():
    """보냈지만 워커가 시작도 못 한 작업의 '전체 다시'·'3D 다시' 표시를 되살린다 (워커가 끊기거나 죽었을 때)."""
    St.full |= St.pending[0]
    St.geo |= St.pending[1]
    St.pending = (False, False)


def submit():
    sc = scene()
    copy = os.path.join(St.dir, f"scene_{St.job % 2}.blend")  # 워커가 아직 읽는 중인 파일을 덮지 않게 번갈아
    os.makedirs(os.path.join(St.dir, "frames"), exist_ok=True)  # 오래된 캐시 정리와 겹쳐 지워졌을 수도 있음
    St.dirty = time.time()  # 저장이 실패하면 디바운스 뒤에 다시 시도 (매 tick 재시도 방지)
    St.copying = True
    try:
        with window_ctx():
            bpy.ops.wm.save_as_mainfile(filepath=copy, copy=True, relative_remap=True,
                                        compress=False, check_existing=False)
    except Exception as e:
        St.error = f"렌더용 복사본 저장 실패: {e}"
        push_state()
        return
    finally:
        St.copying = False
    if St.busy and St.heard and time.time() - St.last_msg > STUCK:
        kill_worker()  # 긴 프레임 하나를 렌더하는 중이라 새 작업을 못 받는 상태: 끊고 새로
    St.job += 1
    job = dict(id=St.job, blend=copy, dir=St.dir, frame=sc.frame_current, full=St.full, geo=St.geo,
               engine=St.engine, res=St.res, scripts=not bpy.app.autoexec_fail)
    # 워커가 이 작업을 실제로 시작했다고 알려 올 때까지 표시를 보관 (그 전에 워커가 끊기면 되살림)
    St.pending = (St.pending[0] or St.full, St.pending[1] or St.geo)
    St.full = St.geo = St.force = St.play_edit = False  # 이 작업이 해시 비교로 모든 변화를 확인한다
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
    St.job_err = False
    St.last_msg = time.time()
    St.error = ""
    St.checking = set(range(sc.frame_start, sc.frame_end + 1))
    push_state()


def on_worker(m):
    t = m["t"]
    if t == "exit":
        if m["p"] is not St.worker:  # 이미 새 워커로 바뀐 뒤 들어온 옛 워커의 종료
            return
    else:
        St.heard = True
        if m.get("dir") != St.dir or (t != "glb" and m.get("id") != St.job):
            # 파일을 바꾸기 전 작업, 또는 새 작업이 이미 나간 뒤 도착한 옛 작업의 결과
            # (그 프레임들은 새 작업이 다시 확인해 버전과 함께 알려 준다. glb 는 파일이 최신이라 받는다)
            return
    St.last_msg = time.time()
    if t in ("start", "glb", "ok", "done") and m.get("id") == St.job:
        # 지금 작업의 결과가 나옴 = '전체 다시' 초기화는 이미 디스크에 저장됨 (프레임 루프 전에 저장하므로)
        # (error 는 파일 열기 단계 실패일 수 있어 확인으로 치지 않는다)
        St.pending = (False, False)
    if t in ("done", "ok"):
        f = m["f"]
        St.frames[f] = m["v"]
        St.checking.discard(f)
        broadcast("frame", json.dumps(dict(f=f, v=St.frames.get(f))))
        St.snap_dirty = True  # 재접속한 브라우저용 스냅샷은 tick 에서 한 번에
    elif t == "glb":
        St.glb = m["v"]
        broadcast("glb", json.dumps(dict(v=St.glb)))
    elif t == "idle":
        St.busy = False
        if not St.job_err:  # 오류로 끊긴 작업이면 확인 못 한 프레임은 계속 '갱신 필요'
            St.checking = set()
        trust_saved()
        push_state()
    elif t == "error":
        St.error = m["msg"]
        St.job_err = True
        restore_pending()  # 시작 전(파일 열기) 실패면 '전체 다시'를 다음 작업으로 넘긴다. 시작 후면 이미 비어 있음
        push_state()
    elif t == "exit":
        St.worker = None
        restore_pending()
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
        ob = sc.objects.get(str(c.get("name")))  # 이 씬에 없는(지워진) 오브젝트는 건드리지 않는다
        if ob is None and sc.camera and sc.camera.name == c.get("name"):
            ob = sc.camera
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
            mark(edit=False)  # 워커가 설정 변경을 보고 전체 렌더
            St.force = True
        if "auto" in c:
            St.auto = bool(c["auto"])
        push_state()
    elif op == "rerender":
        mark(full=True, edit=False)
        St.force = True


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
        playing = is_playing()
        if St.playing and not playing and St.play_edit:
            mark()  # 재생 중에 사람이 옮긴 흔적이 있으면, 멈췄을 때 해시 비교로 바뀐 프레임만 확인
            St.play_edit = False
        St.playing = playing
        if St.sigs_stale and not playing:
            refresh_sigs()
        send = False
        rng = (sc.frame_start, sc.frame_end)
        if rng != St.rng:
            if St.rng is not None:
                mark()  # 범위가 늘면 새 프레임 렌더
            St.rng = rng
            send = True
        # 렌더와 상관없이 화면에만 쓰이는 값(fps·마커)은 바뀌면 상태만 다시 보낸다
        meta = (sc.render.fps, sc.render.fps_base,
                tuple((m.frame, m.name, m.camera and m.camera.name) for m in sc.timeline_markers))
        if meta != St.meta:
            # 카메라가 연결된 마커가 바뀌면(옮김·추가·삭제·카메라 교체) 구간별 활성 카메라가 달라진다 → 렌더 확인
            # (프레임 해시가 프레임마다 활성 카메라를 보므로 바뀐 구간만 다시 렌더됨)
            cams = lambda mt: tuple(x for x in mt[2] if x[2]) if mt else ()
            if St.meta is not None and cams(meta) != cams(St.meta):
                mark()
            St.meta = meta
            send = True
        if sc.frame_current != St.sent_frame:
            St.sent_frame = sc.frame_current
            broadcast("cur", json.dumps(dict(f=sc.frame_current)))
        if St.xf_dirty:
            St.xf_dirty = False
            push_xf()
        if send or St.snap_dirty:  # 프레임 완료는 스냅샷만 새로(이벤트는 이미 보냄), 범위·마커는 탭에도 전송
            push_state(send=send)
        if ((St.auto or St.force) and St.dirty and time.time() - St.dirty > DEBOUNCE
                and not playing and not (in_modal() and time.time() - St.dirty < 10)):
            # 조작 도중엔 기다리되, 다른 애드온이 모달을 계속 띄워 둔 경우에 영원히 멈추지 않게 10초 상한
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
        if p.startswith("/cache/frames/") and St.dir:
            # 요청한 버전이 지금 그 프레임의 버전일 때만 준다: 렌더 중 덮어쓰는 파일이나 다른 파일의 그림이
            # 그 버전 주소로 브라우저에 오래 캐시되면 틀린 그림이 남는다
            name = p[len("/cache/frames/"):]
            try:
                f = int(name.split(".")[0])
            except ValueError:
                return self._send(404, b"not found")
            v = parse_qs(urlparse(self.path).query).get("v", [""])[0]
            if not v or St.frames.get(f) != v:
                return self._send(404, b"stale", cache="no-store")
            return self._file(os.path.join(St.dir, "frames"), name, "max-age=31536000")
        if p.startswith("/cache/") and St.dir:
            return self._file(St.dir, p[len("/cache/"):])
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


# ---------------- 자동 업데이트 · 정리 ----------------

ATTEMPT = os.path.join(ROOT, "update_attempt.txt")


def check_update():
    """백그라운드 스레드. GitHub 최신 릴리스가 더 새 버전이면 zip 을 받아 둔다 (적용은 메인 스레드).
    블렌더 4.2+ 의 '온라인 접근 허용'은 기본이 꺼짐이라 따르면 업데이트가 사실상 안 된다 →
    이 애드온 환경설정의 '자동 업데이트 확인'으로 따로 켜고 끈다. 블렌더를 오프라인 모드로 실행했을 때만 건너뜀"""
    if getattr(bpy.app, "online_access_override", False) and not getattr(bpy.app, "online_access", True):
        return
    if os.path.exists(os.path.join(os.path.dirname(HERE), ".git")):
        return  # 개발 폴더에서 로드한 경우: 작업 중인 코드를 덮지 않는다
    try:
        get = lambda url: urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "blender-live-preview"}), timeout=30)
        rel = json.load(get(f"https://api.github.com/repos/{REPO}/releases/latest"))
        tag = rel["tag_name"]
        latest = tuple(int(x) for x in tag.lstrip("vV").split("."))
        if latest <= tuple(bl_info["version"]):
            return
        try:
            if open(ATTEMPT, encoding="utf-8").read() == tag:
                return  # 이미 적용을 시도했는데 버전이 그대로: 릴리스 zip 의 버전 표기가 틀림, 반복하지 않음
        except OSError:
            pass
        url = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith(".zip"))
        data = get(url).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.testzip() is not None or "live_preview/__init__.py" not in z.namelist():
                raise ValueError("릴리스 zip 이 손상됐거나 형식이 다름")
        os.makedirs(ROOT, exist_ok=True)
        with open(ATTEMPT, "w", encoding="utf-8") as f:
            f.write(tag)
        St.update = (latest, data)
    except Exception as e:  # 오프라인·레이트 리밋·형식 이상: 지금 버전으로 계속
        print("[live_preview] 업데이트 확인 실패:", e)


def apply_update():
    """임시 폴더에 다 푼 뒤 폴더째 바꿔치기. 새 버전이 켜지지 않으면 옛 버전으로 되돌린다."""
    import addon_utils
    latest, data = St.update
    St.update = None
    name, parent = __name__, os.path.dirname(HERE)
    new, bak = HERE + ".new", HERE + ".bak"
    for d in (new, bak):
        shutil.rmtree(d, ignore_errors=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                top, _, rest = n.partition("/")
                if top != "live_preview" or not rest or n.endswith("/") or ".." in rest.split("/"):
                    continue
                dst = os.path.join(new, *rest.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
    except Exception as e:
        print("[live_preview] 업데이트 풀기 실패, 지금 버전 유지:", e)
        shutil.rmtree(new, ignore_errors=True)
        forget_attempt()  # 일시적인 실패(디스크·잠금)일 수 있으니 다음 실행 때 다시 시도
        bpy.app.timers.register(tick, first_interval=0.1, persistent=True)
        return None
    addon_utils.disable(name, default_set=False)
    try:
        os.rename(HERE, bak)
        os.rename(new, HERE)
    except OSError as e:
        print("[live_preview] 업데이트 교체 실패(파일 잠김 등), 지금 버전 유지:", e)
        if not os.path.exists(HERE) and os.path.exists(bak):
            os.rename(bak, HERE)
        shutil.rmtree(new, ignore_errors=True)
        forget_attempt()
        addon_utils.enable(name, default_set=False)
        return None
    if addon_utils.enable(name, default_set=False) is None:  # 새 버전이 켜지지 않음 → 되돌리기
        print("[live_preview] 새 버전 켜기 실패, 이전 버전으로 되돌림")
        try:
            os.rename(HERE, new)  # 깨진 새 버전은 옆으로 치우고 (지우기는 잠긴 파일이 있으면 반만 지워질 수 있음)
            os.rename(bak, HERE)
            shutil.rmtree(new, ignore_errors=True)
        except OSError as e:
            print("[live_preview] 되돌리기 실패 — 애드온을 다시 설치해 주세요:", e)
        addon_utils.enable(name, default_set=False)
    else:
        shutil.rmtree(bak, ignore_errors=True)
        print("[live_preview] 업데이트 완료:", ".".join(map(str, latest)))
    return None


def forget_attempt():
    try:
        os.remove(ATTEMPT)
    except OSError:
        pass


def clean_old_caches():
    for d in (HERE + ".new", HERE + ".bak"):  # 지난 업데이트가 중간에 끊겨 남은 폴더 (지금 버전은 정상 동작 중)
        shutil.rmtree(d, ignore_errors=True)
    cutoff = time.time() - CACHE_DAYS * 86400
    try:
        for d in os.scandir(ROOT):
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d.path, ignore_errors=True)
    except OSError:
        pass


# ---------------- UI ----------------

class LP_OT_open(bpy.types.Operator):
    bl_idname = "live_preview.open"
    bl_label = "미리보기 사이트 열기"

    def execute(self, ctx):
        if St.clients:  # 탭이 늘어날수록 브라우저 연결 한도(6)에 걸려 모든 탭이 멈춘다
            self.report({"WARNING"}, "미리보기가 이미 브라우저에 열려 있습니다 (그 탭을 사용하세요)")
            return {"CANCELLED"}
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return {"FINISHED"}


class LP_OT_rerender(bpy.types.Operator):
    bl_idname = "live_preview.rerender"
    bl_label = "전체 다시 렌더"

    def execute(self, ctx):
        mark(full=True)
        St.force = True
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


class LP_Prefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    auto_update: bpy.props.BoolProperty(
        name="자동 업데이트 확인",
        description="블렌더를 켤 때 GitHub 에서 새 버전을 확인해 자동으로 받아 적용 (블렌더의 '온라인 접근 허용'과 별개)",
        default=True)

    def draw(self, ctx):
        self.layout.prop(self, "auto_update")


classes = (LP_Prefs, LP_OT_open, LP_OT_rerender, LP_OT_auto, LP_PT_panel)
HANDLERS = ((bpy.app.handlers.depsgraph_update_post, on_depsgraph),
            (bpy.app.handlers.frame_change_post, on_frame),
            (bpy.app.handlers.load_post, on_load),
            (bpy.app.handlers.save_post, on_save))


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
    St.busy = False
    bpy.app.timers.register(tick, first_interval=0.5, persistent=True)
    prefs = bpy.context.preferences.addons.get(__name__)
    if prefs is None or prefs.preferences.auto_update:
        threading.Thread(target=check_update, daemon=True).start()
    threading.Thread(target=clean_old_caches, daemon=True).start()


def unregister():
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for lst, fn in HANDLERS:
        if fn in lst:
            lst.remove(fn)
    stop_server()
    kill_worker()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
