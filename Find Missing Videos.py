from pathlib import Path

def organize_files(base=Path.cwd()):

    count = 0

    # Use rglob to safely traverse directories
    undownloaded = ["20.A Fortnite Festival", " Extras\\", "GH World Tour DLC", "Guitar Hero 5 DLC", "GH Warriors of Rock DLC"]
    for path in base.rglob("*"):
        if path.is_file():
            # Handle Renaming
            if path.suffix == '.webm' and path.name != 'video.webm':
                new_path = path.parent / 'video.webm'
                # Only move if destination doesn't exist to prevent data loss
                if not new_path.exists():
                    path.rename(new_path)
                    print(f"Renamed: {path} -> {new_path}")

            # Handle Missing Videos
            elif path.name == 'notes.mid':
                skip_song = False
                for game in undownloaded:
                    if game in str(path):
                        skip_song = True
                if skip_song: continue
                folder = path.parent
                if not (folder / 'video.webm').exists() and not (folder / 'video.mp4').exists():
                    count += 1
                    print(f"Missing: {str(folder)}")

    print(f"Total missing videos: {count}")

if __name__ == '__main__':
    organize_files()
