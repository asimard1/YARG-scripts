from pathlib import Path
from difflib import SequenceMatcher
import shutil
import tqdm

def moveVideos(path: Path):
    print("\n")
    video_source_dir = path / (path.name + " Videos")
    if not video_source_dir.exists():
        return
    print(f"Getting videos from {video_source_dir}.")
    videos_list = sorted(list(video_source_dir.rglob("*.mp4")) + list(video_source_dir.rglob("*.webm")))
    already_found = {}
    potential_dest_list = sorted([file for file in path.rglob("*") if file.is_dir() and str(video_source_dir) not in str(file)])
    print(f"Checking {len(videos_list)} videos against {len(potential_dest_list)} folders.")
    print("Finding best fits...")
    for video in tqdm.tqdm(videos_list): # Finding fits
        song_name = video.parent.name.replace("_extracted", "")
        folder_name = video.parent.parent.name.replace("_extracted", "")
        processed_name = folder_name + " - " + song_name + (f" - {video.stem}" if not video.name.lower().startswith("video.") else "")

        best_sim = 0
        best_folder = ""
        lines = []
        for potential_dest in potential_dest_list:
            processed_potential = f"{potential_dest.parent.name} - {potential_dest.name}"
            similarity = SequenceMatcher(None, processed_name, processed_potential).ratio()
            lines.append(f"{processed_name} -> {processed_potential}, {similarity:.3f}")
            # print(song_name, extracted_folder.name, similarity)
            if similarity > best_sim and not (potential_dest / video.name).exists():
                best_sim = similarity
                best_folder = potential_dest
        if best_folder in already_found or best_folder == "":
            current_video = f"{video.parent.name}\\{video.name}"
            recorded = already_found[best_folder]
            recorded_name = f"{recorded[0].parent.name}\\{recorded[0].name}"
            recorded_sim = recorded[1]
            fitting = best_folder.name if isinstance(best_folder, Path) else "None"

            print(f"\nPROBLEM PROBLEM: {current_video}: found fitting folder: {fitting}")
            if best_folder == "":
                break
            print(f"Was already chosen by: {recorded_name}")
            print(f"Recorded sim: {recorded_sim:.3f}. New sim: {best_sim:.3f}")
            if best_sim < recorded_sim:
                # We don't want to replace
                continue
        print(f"\n\n{processed_name} -> {best_folder.name}")
        # for line in lines:
        #     line_str = line + (" (chosen)" if f"{best_folder.parent.name} - {best_folder.name}" in line else "")
        #     print(line_str)
        if best_folder in already_found:
            print("Replacing...")
        already_found[best_folder] = [video, best_sim]

    print("Moving video around...")
    k = 0
    for best_folder in tqdm.tqdm(already_found): # Moving videos
        video_path = already_found[best_folder][0]
        dest_file = best_folder / video_path.name
        if dest_file.is_file() or dest_file.is_dir():
            print(f"PROBLEM PROBLEM {dest_file} exists")
            print("Is it a file?:", dest_file.is_file())
            print("Is it a directory?:", dest_file.is_dir())
            print("File size in bytes:", dest_file.stat().st_size if dest_file.exists() else "N/A")
            continue
        # shutil.move(video_path, best_folder / video_path.name)
        k += 1
    print(f"Moved {k} videos.")


    print("\n")

if __name__ == "__main__":
    moveVideos(Path.cwd())