"""백그라운드 렌더 워커. 애드온이 `blender -b --python worker.py` 로 띄운다.
stdin: 작업 JSON 한 줄씩 / stdout: '@@' + 결과 JSON 한 줄씩.
프레임마다 장면 상태 해시를 비교해 바뀐 프레임만 렌더한다."""
import bpy
import os
import sys
import json
import time
import queue
import numpy
import shutil
import hashlib
import threading

jobs = queue.Queue()


def _read():
    for line in sys.stdin:
        if line.strip():
            jobs.put(json.loads(line))
    os._exit(0)  # 애드온(부모 블렌더)이 닫히면 같이 종료


threading.Thread(target=_read, daemon=True).start()


CUR = {}  # 지금 처리 중인 작업. 메시지에 작업 번호·폴더를 붙여 애드온이 지난 작업 결과를 걸러내게 한다


def emit(**kw):
    print("@@" + json.dumps(dict(kw, id=CUR.get("id"), dir=CUR.get("dir"))), flush=True)


ENGINES = {"WORKBENCH": ["BLENDER_WORKBENCH"],
           "EEVEE": ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"],  # 4.2~4.x 는 _NEXT, 4.1·5.x 는 BLENDER_EEVEE
           "CYCLES": ["CYCLES"]}


def set_engine(r, name):
    for e in ENGINES[name]:
        try:
            r.engine = e
            return
        except TypeError:
            pass


def m2s(m):
    return ",".join(f"{v:.5f}" for row in m for v in row)


def frame_hash(sc):
    """렌더 결과에 영향을 주는 프레임별 상태. 같으면 다시 렌더하지 않는다."""
    # ponytail: 재질 값 애니메이션, 뷰포트에서 꺼둔 오브젝트의 애니메이션은 못 잡음 → '전체 다시 렌더' 버튼
    dg = bpy.context.evaluated_depsgraph_get()
    h = hashlib.md5((sc.camera.name if sc.camera else "").encode("utf-8"))
    for ob in sc.objects:
        # 모디파이어·시뮬레이션·지오노드로 시간에 따라 변하는 형태. 메시는 평가된 정점 좌표로,
        # 지오노드 인스턴스·파티클처럼 정점으로 안 잡히는 것은 프레임 번호를 넣어 매 프레임 다르게 본다
        if not (ob.modifiers or ob.particle_systems):
            continue
        e = ob.evaluated_get(dg)
        if ob.particle_systems or any(m.type == "NODES" for m in ob.modifiers) or ob.type != "MESH":
            h.update(f"{ob.name}@{sc.frame_current}".encode("utf-8"))
        else:
            co = numpy.empty(len(e.data.vertices) * 3, dtype=numpy.float32)
            e.data.vertices.foreach_get("co", co)
            h.update(co.round(4).tobytes())
    for c in bpy.data.collections:
        h.update(f"{c.name}{c.hide_render}".encode("utf-8"))
    for ob in sc.objects:
        e = ob.evaluated_get(dg)
        h.update(f"{ob.name}{ob.hide_render}{m2s(e.matrix_world)}".encode("utf-8"))
        if e.pose:
            h.update("".join(m2s(pb.matrix) for pb in e.pose.bones).encode())
        keys = getattr(e.data, "shape_keys", None)
        if keys:
            h.update(",".join(f"{k.value:.4f}" for k in keys.key_blocks).encode())
        if ob.type == "CAMERA":
            h.update(f"{e.data.lens:.4f}{e.data.ortho_scale:.4f}".encode())
        elif ob.type == "LIGHT":
            h.update(f"{e.data.energy:.4f}{tuple(e.data.color)}".encode())
    return h.hexdigest()


def export_glb(sc, frame, d):
    sc.frame_set(frame)
    # ponytail: 텍스처는 빼고(기본색만) 가볍게. 구도용이라 충분, 필요하면 export_image_format 변경
    want = dict(filepath=os.path.join(d, "scene.glb"), export_format="GLB", use_renderable=True,
                export_apply=True, export_animations=False, export_skins=False, export_morph=False,
                export_cameras=False, export_lights=False, export_yup=True, export_image_format="NONE")
    props = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
    bpy.ops.export_scene.gltf(**{k: v for k, v in want.items() if k in props})
    emit(t="glb", v=str(time.time()))


def save(path, data):
    tmp = path + ".tmp"  # 쓰는 도중에 애드온이 읽어도 깨진 파일을 보지 않게
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def run(job):
    d = job["dir"]
    hp = os.path.join(d, "hashes.json")
    try:
        with open(hp, encoding="utf-8") as f:
            hashes = json.load(f)
    except (OSError, ValueError):
        hashes = {}

    bpy.ops.wm.open_mainfile(filepath=job["blend"], load_ui=False)
    sc = bpy.context.scene
    r = sc.render
    set_engine(r, job["engine"])
    r.resolution_percentage = job["res"]
    r.use_sequencer = False  # VSE 가 있어도 3D 장면을 렌더
    if job["engine"] == "WORKBENCH":
        r.use_compositing = False
    s = r.image_settings
    if hasattr(s, "media_type"):  # 5.x: 영상 출력으로 설정된 파일 대비
        s.media_type = "IMAGE"
    s.file_format = "JPEG"
    s.color_mode = "RGB"
    s.quality = 85
    r.use_file_extension = True

    key = f'{job["engine"]}/{job["res"]}/{r.resolution_x}x{r.resolution_y}'
    if job["full"] or hashes.get("_settings") != key:
        # 세대값: 재질·형태 수정처럼 프레임 상태 해시에 안 잡히는 변경도 버전이 바뀌게
        hashes = {"_settings": key, "_gen": str(time.time())}
        save(hp, hashes)  # 중간에 끊겨도 '전체 다시'가 유지되게 바로 저장

    if job["geo"]:
        export_glb(sc, job["frame"], d)

    lo, hi = sc.frame_start, sc.frame_end
    cur = min(max(job["frame"], lo), hi)
    for f in sorted(range(lo, hi + 1), key=lambda f: (abs(f - cur), f < cur)):  # 현재 프레임부터 바깥으로
        if not jobs.empty():
            return  # 새 편집이 들어왔으면 그만두고 새 작업으로
        sc.frame_set(f)
        h = hashlib.md5((key + hashes.get("_gen", "") + frame_hash(sc)).encode()).hexdigest()
        out = os.path.join(d, "frames", f"{f:05d}")
        if hashes.get(str(f)) == h and os.path.exists(out + ".jpg"):
            emit(t="ok", f=f)
            continue
        same = next((k for k, v in hashes.items() if v == h and os.path.exists(os.path.join(d, "frames", f"{int(k):05d}.jpg"))), None)
        if same:  # 정지 구간처럼 상태가 같은 프레임은 렌더 없이 복사
            shutil.copyfile(os.path.join(d, "frames", f"{int(same):05d}.jpg"), out + ".jpg")
        else:
            r.filepath = out
            bpy.ops.render.render(write_still=True)
        hashes[str(f)] = h
        save(hp, hashes)
        emit(t="done", f=f, v=h[:8])


while True:
    job = jobs.get()
    while not jobs.empty():  # 밀린 작업은 최신 하나로 합친다
        nxt = jobs.get()
        nxt["full"] |= job["full"] and nxt["dir"] == job["dir"]
        nxt["geo"] |= job["geo"] and nxt["dir"] == job["dir"]
        job = nxt
    CUR.clear()
    CUR.update(id=job["id"], dir=job["dir"])
    try:
        run(job)
    except Exception as e:
        emit(t="error", msg=f"{type(e).__name__}: {e}")
    if jobs.empty():
        emit(t="idle")
