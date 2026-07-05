from pathlib import Path
import time
import shutil
CON_MAGIC = (b"CON ", b"LIVE", b"PIRS")
def _check_magic(path: Path, magics: tuple[bytes, ...]) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(max(len(m) for m in magics))
            return any(head.startswith(m) for m in magics)
    except Exception:
        return False
def is_con_file(path: Path) -> bool: return _check_magic(path, CON_MAGIC)

EXTENSION_LIST = [".ogg", ".opus", ".mp3", ".wav"]
EXCLUDED_CON_SUFFIXES = frozenset(EXTENSION_LIST + [".ini", ".mid", ".bak", ".webm", ".mp4", ".png"])


def moveCons(root: Path, dest: Path) -> None:
    t0 = time.perf_counter()
    cons = sorted(
            p for p in root.rglob("*")
            if p.is_file()
            and p.suffix not in EXCLUDED_CON_SUFFIXES
            and is_con_file(p)
        )
    list = []
    print(f"Scan took {time.perf_counter() - t0:.1f} seconds")
    for confile in cons:
        confile_rel_path = confile.relative_to(root)
        confile_new_path = dest / confile_rel_path
        list.append((confile, confile_new_path))
        confile_new_dir = confile_new_path.parent
        if not confile_new_dir.exists():
            confile_new_dir.mkdir(parents=True, exist_ok=True)
        if not confile_new_path.is_file():
            print(f"{confile} ---> {confile_new_path}")
            shutil.move(confile, confile_new_path)

if __name__ == "__main__":
    moveCons(Path.cwd(), Path.cwd().parent / "CON FILES BACKUP")