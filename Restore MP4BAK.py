import shutil
from pathlib import Path

for i, file in enumerate(Path.cwd().rglob("*.mp4.bak")):
    folder = file.parent
    dest = folder / file.stem
    shutil.move(file, dest)
print(i + 1)