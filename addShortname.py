from pathlib import Path
import json
import time
import tqdm

def scanDir(root: Path):
    songs_updates_dir = list(root.rglob("songs_updates"))[0]
    songs_updates = [f.name for f in songs_updates_dir.rglob("*") if f.is_dir()]
    print(len(songs_updates))

    inUpdatesRB1 = 0
    inUpdatesRB2 = 0
    inUpdatesRB3 = 0
    inUpdatesOther = 0
    notInUpdates = 0
    updated = 0
    toUpdate = list(root.rglob("dta_meta_debug.json"))
    for dta_meta_debug in tqdm.tqdm(toUpdate):
        with open(dta_meta_debug, 'r', encoding='utf-8') as file:
            dta_meta_debug_dict = json.load(file)
        try:
            shortname = '/'.join(dta_meta_debug_dict.get('song').get('name').split('/')[2:])
        except:
            print(f"Problem reading name from {dta_meta_debug}")
            continue
        # print(shortname)
        try:
            assert shortname in songs_updates
            # print(f"HAPPY HAPPY In updates: {shortname} ------------")
            if '13.' in str(dta_meta_debug):
                inUpdatesRB1 += 1
            elif '14.' in str(dta_meta_debug):
                inUpdatesRB2 += 1
            elif '18.' in str(dta_meta_debug):
                inUpdatesRB3 += 1
            else:
                inUpdatesOther += 1
        except:
            # print(f"Not in updates: {shortname}")
            notInUpdates += 1
        iniPath = dta_meta_debug.parent / "song.ini"
        if not iniPath.is_file():
            print(f"{iniPath} not found.")
            continue
        # --- Read the lines ---
        with open(iniPath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        shortname_found = False
        section_found = False
        insert_index = -1

        for i, line in enumerate(lines):
            clean_line = line.strip()

            # Keep track of when we enter the [song] section
            if clean_line.lower() == '[song]':
                section_found = True
                continue
            if section_found:
                insert_index = i + 1 # Save this spot in case we need to insert it

            # If we find another section header, we are no longer in [song]
            if section_found and clean_line.startswith('[') and clean_line.endswith(']'):
                section_found = False

            # Check for the shortname key ONLY if we are inside the [song] section
            if section_found and clean_line.startswith('shortname'):
                shortname_found = True
                foundShortname = clean_line.split('=')[-1].strip()

                if foundShortname == shortname:
                    break # Already correct, nothing to change

                print(f"Changing shortname in {iniPath.name}: {foundShortname} -> {shortname}")
                lines[i] = f"shortname = {shortname}\n" # Correctly targets index 'i' from the main lines list
                break

        # If it wasn't found anywhere in the [song] section
        if not shortname_found:
            if insert_index >= 0:
                # print(f'Inserting shortname at index {insert_index}')
                # Insert it cleanly right under the [song] header
                lines.insert(insert_index, f"shortname = {shortname}\n")
            else:
                # print(f'Appending shortname')
                # Fallback: [song] section literally wasn't in the file, append it
                lines.append(f"\n[song]\nshortname = {shortname}\n")
            updated += 1

        # print(lines)
        # --- Write the lines back ---
        with open(iniPath, 'w', encoding='utf-8') as f:
            f.writelines(lines)

    print(f"Have updates: {inUpdatesRB1}, {inUpdatesRB2}, {inUpdatesRB3}, {inUpdatesOther}, don't have updates: {notInUpdates}.")
    print(f"Updated ini files: {updated}.")



if __name__ == "__main__":
    t0 = time.perf_counter()
    scanDir(Path.cwd())
    print(f"Operation took {time.perf_counter() - t0: .2f} seconds.")