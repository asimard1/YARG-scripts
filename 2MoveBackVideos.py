from pathlib import Path
from difflib import SequenceMatcher
import shutil
import tqdm

def moveVideos(path: Path):
    print("\n")
    video_source_dir = path / (path.name + " Videos")
    if not video_source_dir.exists():
        return
    print(f"Getting videos from {video_source_dir}")
    videos_list = list(video_source_dir.rglob("*.mp4")) + list(video_source_dir.rglob("*.webm"))
    already_found = {}
    for video in tqdm.tqdm(videos_list):
        video_folder = video.parent
        song_name = video_folder.name.replace("_extracted", "")
        # print(song_name)

        best_sim = 0
        k = 0
        best_folder = ""
        extracted_list = [file for file in path.rglob("*") if file.is_dir() and str(video_source_dir) not in str(file)]
        for extracted_folder in extracted_list:
            similarity = SequenceMatcher(None, song_name, extracted_folder.name).ratio()
            # print(song_name, extracted_folder.name, similarity)
            if similarity > best_sim and not (extracted_folder / video.name).exists():
                best_sim = similarity
                best_folder = extracted_folder
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
        if best_folder in already_found:
            print("Replacing...")
        already_found[best_folder] = [video, best_sim]
    for best_folder in tqdm.tqdm(already_found):
        video_path = already_found[best_folder][0]
        dest_file = best_folder / video_path.name
        if dest_file.is_file() or dest_file.is_dir():
            print(f"PROBLEM PROBLEM {dest_file} exists")
            print("Is it a file?:", dest_file.is_file())
            print("Is it a directory?:", dest_file.is_dir())
            print("File size in bytes:", dest_file.stat().st_size if dest_file.exists() else "N/A")
            continue
        shutil.move(video_path, best_folder / video_path.name)

    # printed = False
    # for extracted in extracted_list:
    #     if extracted not in already_found:
    #         if not printed:
    #             print("Need to check the following:")
    #             printed = True
    #         print(extracted)
    print("\n")

if __name__ == "__main__":
    moveVideos(Path.cwd())