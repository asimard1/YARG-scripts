import shutil
from pathlib import Path

k = 0
for i, file in enumerate(Path.cwd().rglob("*.mp4.bak")):
    folder = file.parent
    dest = folder / file.stem
    shutil.move(file, dest)
    webm = file.parent / (dest.stem + ".webm")
    if webm.exists():
        k += 1
        webm.unlink()
print(f"{i + 1} files restored.")
print(f"{k} webms deleted.")