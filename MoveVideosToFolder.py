"""
This file should be placed directly in the folder of a particular game to work.
"""

from pathlib import Path
import shutil

def MoveVideos(path: Path):
    dir_name = path.name
    # Will get created later on
    videos_dir = path / (dir_name + " Videos")

    videos_list = list(path.rglob("*.mp4")) + list(path.rglob("*.webm"))
    print(len(videos_list))
    k = 0
    for video in videos_list:
        relative_path = str(video).replace(str(path), "")[1:]
        dest_file = videos_dir / relative_path
        dest_dir = dest_file.parent
        if not dest_dir.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            if not dest_file.exists():
                k += 1
                shutil.move(video, dest_file)
            else:
                print(f"Already existed: {dest_file}")
    print(k)

if __name__ == "__main__":
    print("This file should be placed directly in the folder of a particular game to work.")
    MoveVideos(Path.cwd())