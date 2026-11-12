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
import addon_utils

jobs = queue.Queue()


def _read():
    for line in sys.stdin:
        if line.strip():
            jobs.put(json.loads(line))
    os._exit(0)  # 애드온(부모 블렌더)이 닫히면 같이 종료


threading.Thread(target=_read, daemon=True).start()
addon_utils.enable("io_scene_gltf2", default_set=False)  # 환경설정에서 꺼져 있어도 3D 화면용 내보내기는 되게


CUR = {}  # 지금 처리 중인 작업. 메시지에 작업 번호·폴더를 붙여 애드온이 지난 작업 결과를 걸러내게 한다


def emit(**kw):
    print("@@" + json.dumps(dict(kw, id=CUR.get("id"), dir=CUR.get("dir"))), flush=True)


ENGINES = {"WORKBENCH": ["BLENDER_WORKBENCH"],
           "EEVEE": ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"],  # 4.2~4.x 는 _NEXT, 4.1·5.x 는 BLENDER_EEVEE
           "CYCLES": ["CYCLES"]}
SIM_MODS = {"CLOTH", "SOFT_BODY", "FLUID", "DYNAMIC_PAINT", "COLLISION", "NODES"}  # 지오노드: 시뮬레이션 존 가능성
GP_TYPES = {"GPENCIL", "GREASEPENCIL"}  # 4.x / 4.3+


def set_engine(r, name):
    for e in ENGINES[name]:
        try:
            r.engine = e
            return
        except TypeError:
            pass


def m2s(m):
    return ",".join(f"{v:.5f}" for row in m for v in row)


def rna_str(s):
    """구조체의 단순 값(숫자·불·열거·문자열) 전부. 속성을 하나하나 나열하면 빠뜨리기 쉬워서."""
    out = []
    for p in s.bl_rna.properties:
        if p.identifier == "rna_type" or p.type in {"POINTER", "COLLECTION"}:
            continue
        v = getattr(s, p.identifier, None)
        out.append(str(tuple(v) if getattr(p, "is_array", False) else v))
    return ",".join(out)


def frame_hash(sc):
    """렌더 결과에 영향을 주는 프레임별 상태. 같으면 다시 렌더하지 않는다."""
    # ponytail: 재질 값 애니메이션, 뷰포트에서 꺼둔 오브젝트의 애니메이션은 못 잡음 → '전체 다시 렌더' 버튼
    dg = bpy.context.evaluated_depsgraph_get()
    h = hashlib.md5((sc.camera.name if sc.camera else "").encode("utf-8"))
    for ob in sc.objects:
        # 모디파이어·시뮬레이션·지오노드·그리스펜슬처럼 시간에 따라 형태가 변하는 것. 메시는 평가된 정점으로,
        # 정점으로 안 잡히는 것(지오노드 인스턴스·파티클·그리스펜슬 드로잉)은 프레임 번호를 넣어 매 프레임 다르게 본다
        if not (ob.modifiers or ob.particle_systems or ob.type in GP_TYPES):
            continue
        e = ob.evaluated_get(dg)
        if (ob.particle_systems or ob.type != "MESH" or any(m.type == "NODES" for m in ob.modifiers)):
            h.update(f"{ob.name}@{sc.frame_current}".encode("utf-8"))
        else:
            co = numpy.empty(len(e.data.vertices) * 3, dtype=numpy.float32)
            e.data.vertices.foreach_get("co", co)
            h.update(co.round(4).tobytes())
    for c in bpy.data.collections:
        h.update(f"{c.name}{c.hide_render}".encode("utf-8"))
    for ob in sc.objects:
        if ob.type == "CAMERA" and ob != sc.camera:
            continue  # 이 프레임에 안 쓰이는 카메라는 그림에 영향 없음 (카메라 여러 대 장면에서 다른 구간까지 다시 렌더하지 않게)
        e = ob.evaluated_get(dg)
        ic = ob.instance_collection.name if ob.instance_collection else ""
        h.update(f"{ob.name}{ob.hide_render}{ob.instance_type}{ic}{m2s(e.matrix_world)}".encode("utf-8"))
        if e.pose:
            h.update("".join(m2s(pb.matrix) for pb in e.pose.bones).encode())
        keys = getattr(e.data, "shape_keys", None)
        if keys:
            h.update(",".join(f"{k.value:.4f}" for k in keys.key_blocks).encode())
        if ob.type == "CAMERA":
            dof = e.data.dof
            fo = dof.focus_object.name if dof.focus_object else ""  # 객체를 그대로 문자열로 하면 메모리 주소가 들어감
            h.update(f"{rna_str(e.data)}{rna_str(dof)}{fo}".encode("utf-8"))
        elif ob.type == "LIGHT":
            h.update(rna_str(e.data).encode("utf-8"))  # 애니메이션된 라이트 값 (직접 수정은 애드온이 전체 렌더로 보냄)
    return h.hexdigest()


def has_sim(sc):
    """앞 프레임 결과를 이어받아 계산하는 시뮬레이션이 있는지 (있으면 처음부터 순서대로 렌더)"""
    if sc.rigidbody_world:
        return True
    return any(m.type in SIM_MODS for ob in sc.objects for m in ob.modifiers) or \
        any(ob.particle_systems for ob in sc.objects)


def mute_file_outputs(sc):
    """컴포지터 File Output 노드가 작업자의 실제 출력 폴더에 미리보기 결과를 덮어쓰지 않게 끈다."""
    for tree in (getattr(sc, "node_tree", None), getattr(sc, "compositing_node_group", None)):
        if tree:
            for n in tree.nodes:
                if n.type == "OUTPUT_FILE":
                    n.mute = True


def export_glb(sc, frame, d):
    if sc.frame_current != frame:
        sc.frame_set(frame)
    # 구도용이라 형태만: 텍스처·노멀·UV 를 빼고 Draco 로 압축 (200만 버텍스 장면 213MB → 2MB, 5.7초 → 3.3초).
    # 크면 브라우저가 불러오는 순간 영상이 멈칫한다. 노멀이 없으면 3D 화면은 면 단위 음영으로 그린다
    want = dict(filepath=os.path.join(d, "scene.glb"), export_format="GLB", use_renderable=True,
                export_apply=True, export_animations=False, export_skins=False, export_morph=False,
                export_cameras=False, export_lights=False, export_yup=True, export_image_format="NONE",
                export_normals=False, export_texcoords=False,
                export_draco_mesh_compression_enable=True, export_draco_mesh_compression_level=3)
    try:
        names = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
        bpy.ops.export_scene.gltf(**{k: v for k, v in want.items() if k in names})
        emit(t="glb", v=str(time.time()))
    except Exception as e:  # 3D 화면이 안 되더라도 렌더는 계속
        print("glb 내보내기 실패:", e)


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

    # 작업용 블렌더에서 스크립트(파이썬 드라이버)가 허용된 파일이면 여기서도 허용
    bpy.ops.wm.open_mainfile(filepath=job["blend"], load_ui=False, use_scripts=job.get("scripts", False))
    sc = bpy.context.scene
    r = sc.render
    set_engine(r, job["engine"])
    r.resolution_percentage = job["res"]
    r.use_sequencer = False  # VSE 가 있어도 3D 장면을 렌더
    if job["engine"] == "WORKBENCH":
        r.use_compositing = False
    mute_file_outputs(sc)
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
    emit(t="start")  # 파일 열기·초기화 끝: 애드온이 '살아 있고 이 작업을 시작함'을 알게 (첫 프레임이 길어도)

    lo, hi = sc.frame_start, sc.frame_end
    cur = min(max(job["frame"], lo), hi)
    glb_after_first = False
    if has_sim(sc):
        order = range(lo, hi + 1)  # 시뮬레이션은 앞 프레임을 이어서 계산하므로 처음부터 순서대로
        if job["geo"]:
            export_glb(sc, lo, d)  # 어차피 처음 프레임부터 도니 거기서 (프레임을 건너뛰면 시뮬레이션이 다시 계산됨)
    else:
        order = sorted(range(lo, hi + 1), key=lambda f: (abs(f - cur), f < cur))  # 현재 프레임부터 바깥으로
        glb_after_first = job["geo"]  # 지금 보는 프레임을 먼저 보여 주고 3D 화면용 모델은 그다음에
    by_hash = {v: int(k) for k, v in hashes.items() if not k.startswith("_")}
    on_disk = {k for k in hashes if not k.startswith("_")}  # hashes.json 에 이미 적혀 있는 프레임
    saved = time.time()
    try:
        for i, f in enumerate(order):
            if glb_after_first and i == 1:
                export_glb(sc, cur, d)
                glb_after_first = False
            if not jobs.empty():
                return  # 새 편집이 들어왔으면 그만두고 새 작업으로
            sc.frame_set(f)
            h = hashlib.md5((key + hashes.get("_gen", "") + frame_hash(sc)).encode()).hexdigest()
            out = os.path.join(d, "frames", f"{f:05d}")
            if hashes.get(str(f)) == h and os.path.exists(out + ".jpg"):
                emit(t="ok", f=f, v=h[:8])
                continue
            # 그림을 덮어쓰기 전에 이 프레임의 옛 해시를 먼저 지운다: 옛 해시가 새 그림을 가리키면
            # 다른 프레임이 엉뚱한 그림을 복사해 가거나, 강제 종료 뒤 틀린 그림이 '최신'으로 남는다
            old = hashes.pop(str(f), None)
            if old is not None and by_hash.get(old) == f:
                del by_hash[old]
            if str(f) in on_disk:
                save(hp, hashes)
                on_disk.discard(str(f))
            same = by_hash.get(h)
            src = same is not None and os.path.join(d, "frames", f"{same:05d}.jpg")
            # 임시 이름에 쓴 뒤 바꿔 끼운다: 쓰는 도중의 반쪽 그림을 브라우저가 받아 가지 않게
            if src and os.path.exists(src):  # 정지 구간처럼 상태가 같은 프레임은 렌더 없이 복사
                shutil.copyfile(src, out + "_tmp.jpg")
            else:
                r.filepath = out + "_tmp"
                bpy.ops.render.render(write_still=True)
                if not os.path.exists(out + "_tmp.jpg") and r.use_compositing:
                    # 컴포지터에 최종 출력 노드가 없으면(파일 출력 노드만 있는 경우 등) 그림이 안 나온다 → 컴포지팅 없이
                    r.use_compositing = False
                    bpy.ops.render.render(write_still=True)
                if not os.path.exists(out + "_tmp.jpg"):
                    raise RuntimeError(f"{f}프레임 렌더 결과 파일이 만들어지지 않았습니다")
            os.replace(out + "_tmp.jpg", out + ".jpg")
            hashes[str(f)] = h
            by_hash[h] = f
            if time.time() - saved > 1:  # 긴 범위에서 매 프레임 전체를 다시 쓰지 않게
                save(hp, hashes)
                on_disk = {k for k in hashes if not k.startswith("_")}
                saved = time.time()
            emit(t="done", f=f, v=h[:8])
    finally:
        save(hp, hashes)
        if glb_after_first:  # 프레임이 하나뿐이거나 첫 프레임 직후 끊긴 경우에도 3D 모델 갱신은 빠뜨리지 않는다
            export_glb(sc, cur, d)


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
