"""새 버전 배포. live_preview/__init__.py 의 bl_info "version" 을 올린 뒤 실행:
    python release.py "바뀐 내용 한 줄"
zip 을 만들고 GitHub 에 올려 릴리스를 만든다. 작업자 블렌더는 다음에 켤 때 자동 업데이트된다."""
import re
import sys
import zipfile
import pathlib
import subprocess

root = pathlib.Path(__file__).resolve().parent
src = (root / "live_preview" / "__init__.py").read_text(encoding="utf-8")
ver = ".".join(re.search(r'"version": \((\d+), (\d+), (\d+)\)', src).groups())
notes = sys.argv[1] if len(sys.argv) > 1 else f"v{ver}"

zp = root / "live_preview.zip"
with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted((root / "live_preview").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            z.write(p, p.relative_to(root).as_posix())

run = lambda *a: subprocess.run(a, cwd=root, check=True)
run("git", "add", "-A")
if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode:
    run("git", "commit", "-m", f"v{ver}: {notes}")
run("git", "push")
run("gh", "release", "create", f"v{ver}", str(zp), "--title", f"v{ver}", "--notes", notes)
print(f"배포 완료: v{ver}")
