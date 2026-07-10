from pathlib import Path
import time

def repairIni(iniPath: Path) -> bool:
    """Merges a duplicate trailing [song] section back into the first one.
    Returns True if the file was changed."""
    with open(iniPath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    # Find every line that is exactly a [song] header (case-insensitive)
    song_header_indices = [i for i, line in enumerate(lines) if line.strip().lower() == '[song]']

    if len(song_header_indices) <= 1:
        return False  # Nothing to repair

    # Keep the first [song] section as-is. Collect key=value lines from every
    # subsequent [song] section (stopping each at the next section header or EOF).
    first_header = song_header_indices[0]
    extra_lines = []
    cut_ranges = []  # (start, end) index ranges to remove from `lines`, end-exclusive

    for header_idx in song_header_indices[1:]:
        start = header_idx
        end = header_idx + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                break
            end += 1
        # Grab non-blank content lines from this duplicate section
        for line in lines[header_idx + 1:end]:
            if line.strip():
                extra_lines.append(line if line.endswith('\n') else line + '\n')
        cut_ranges.append((start, end))

    # Remove the duplicate section(s), working from the end so earlier indices stay valid
    new_lines = lines[:]
    for start, end in sorted(cut_ranges, reverse=True):
        del new_lines[start:end]
        # Also drop a single blank line immediately preceding the removed header, if present
        if start > 0 and new_lines[start - 1].strip() == '':
            del new_lines[start - 1]

    # Figure out where the first [song] section ends, to insert merged keys there
    insert_at = first_header + 1
    while insert_at < len(new_lines):
        stripped = new_lines[insert_at].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            break
        insert_at += 1

    # Existing keys in the first section (so we don't duplicate, e.g. two 'shortname' lines)
    existing_keys = set()
    for line in new_lines[first_header + 1:insert_at]:
        stripped = line.strip()
        if stripped and '=' in stripped:
            existing_keys.add(stripped.split('=')[0].strip().lower())

    to_add = []
    for line in extra_lines:
        key = line.strip().split('=')[0].strip().lower()
        if key not in existing_keys:
            to_add.append(line)
            existing_keys.add(key)

    new_lines[insert_at:insert_at] = to_add

    with open(iniPath, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    return True


def scanDir(root: Path):
    fixed = 0
    checked = 0
    for iniPath in root.rglob("song.ini"):
        checked += 1
        try:
            if repairIni(iniPath):
                print(f"Fixed: {iniPath}")
                fixed += 1
        except Exception as e:
            print(f"Problem repairing {iniPath}: {e}")
    print(f"Checked {checked} files, fixed {fixed}.")


if __name__ == "__main__":
    t0 = time.perf_counter()
    scanDir(Path.cwd())
    print(f"Operation took {time.perf_counter() - t0: .2f} seconds.")
