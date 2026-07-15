from pathlib import Path
import tqdm

def scan(root: Path = Path.cwd()):
    ini_files = list(root.rglob("song.ini"))
    for ini_file in ini_files:
        name_short_dta: list[str | None] = [None, None, None]
        found = [False, False, False]
        for equal in [' = ', ' =', '= ', '=']:
            with open(ini_file, 'r', encoding='utf-8') as file:
                for line in file:
                    line_small = line.replace('\n', '')
                    if line_small.startswith(f"name{equal}") and not found[0]:
                        found[0] = True
                        name_short_dta[0] = equal.join(line_small.split(equal)[1:]).strip()
                    if line_small.startswith(f"shortname{equal}") and not found[1]:
                        found[1] = True
                        name_short_dta[1] = equal.join(line_small.split(equal)[1:]).strip()
                    if line_small.startswith(f"dtaname{equal}") and not found[2]:
                        found[2] = True
                        name_short_dta[2] = equal.join(line_small.split(equal)[1:]).strip()
        name, shortname, dtaname = name_short_dta
        if name is None:
            print(f"ERROR with {ini_file}")
            return
        if shortname is None and dtaname is None:
            continue
        # print(name, shortname, dtaname)
        if name == shortname or name == dtaname:
            print(ini_file.parent, '--', shortname, '--', dtaname)

if __name__ == "__main__":
    scan(Path.cwd())