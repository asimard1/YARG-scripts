import shutil
import subprocess
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures
import os
import threading
import queue


def convert_mp4_to_webm(
    input_path: Path,
    slot_pool: queue.Queue,
    progress: dict,
    progress_lock: threading.Lock,
    cancel_event: threading.Event,
) -> tuple[bool, str]:
    """Convert an MP4 to WebM and rename the original to .mp4.bak."""
    output_path = input_path.with_suffix(".webm")
    backup_path = input_path.with_suffix(".mp4.bak")

    if cancel_event.is_set():
        return False, f"✗ Skipped (cancelled): {input_path.parent.name}/{input_path.name}"

    worker_id = slot_pool.get()

    try:
        if cancel_event.is_set():
            return False, f"✗ Skipped (cancelled): {input_path.parent.name}/{input_path.name}"

        # if output_path.exists():
        #     return False, f"✗ Failed: Skipping {input_path.name}: output already exists."

        if backup_path.exists():
            return False, f"✗ Failed: Skipping {input_path.name}: backup already exists."

        command = ["ffmpeg",
                    "-i", str(input_path),
                    "-map", "0",
                      "-c:v", "libvpx",
                   "-deadline", "best", 
                   "-cpu-used", "0", 
                   "-crf", "5",
                   "-b:v", "4M", 
                   "-minrate", "3M", 
                   "-maxrate", "7M",
                   "-c:a", "libopus", 
                   "-b:a", "192k",
                   "-progress", "pipe:1", 
                   "-nostats", 
                   "-loglevel", "error", 
                   "-y",
                   str(output_path)
                   ]

        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(input_path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            duration = float(probe.stdout.strip())
            if duration <= 0:
                raise ValueError("Could not determine input duration")

            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
            if process.stdout is not None:
                for line in process.stdout:
                    if "=" not in line:
                        continue
                    key, value = line.strip().split("=", 1)
                    if key == "out_time_ms":
                        pct = min(float(value) / 1_000_000 / duration, 1.0)
                        with progress_lock:
                            progress[worker_id] = (
                                f"{input_path.parent.name}/{input_path.name}",
                                pct,
                            )
            process.wait()
            stderr_output = process.stderr.read() if process.stderr else ""
            if process.returncode != 0:
                raise subprocess.CalledProcessError(
                    process.returncode, command, stderr=stderr_output or "Unknown FFmpeg error"
                )

            # Verify the output is a valid, complete file before trusting it enough
            # to overwrite the original — a 0-return-code doesn't guarantee that.
            verify = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
            )
            out_duration = float(verify.stdout.strip() or 0)
            if verify.returncode != 0 or out_duration < duration * 0.98:
                raise subprocess.CalledProcessError(
                    1,
                    command,
                    stderr=f"Output duration mismatch or unreadable ({out_duration:.1f}s vs {duration:.1f}s expected)",
                )

            if cancel_event.is_set():
                if output_path.exists():
                    output_path.unlink()
                return False, f"✗ Skipped (cancelled): {input_path.parent.name}/{input_path.name}"

            # shutil.move(input_path, backup_path)
            return True, f"✓ Success ({backup_path.parent.name}/{backup_path.name} created)"

        except KeyboardInterrupt:
            if output_path.exists():
                output_path.unlink()
            print("\nInterrupted.")
            raise

        except subprocess.CalledProcessError as exc:
            if output_path.exists():
                output_path.unlink()

            error = exc.stderr.strip() if exc.stderr else "Unknown FFmpeg error"
            return False, f"✗ Failed: {input_path.parent.name}/{input_path.name}: {error}"

        except Exception as exc:
            if output_path.exists():
                output_path.unlink()
            return False, f"✗ Failed: {input_path.parent.name}/{input_path.name}: {exc}"

    finally:
        with progress_lock:
            progress.pop(worker_id, None)
        slot_pool.put(worker_id)


def process_directory(
    directory: Path = Path.cwd(),
    max_workers: int | None = None,
) -> None:
    """Recursively convert every MP4 that has not already been converted."""
    print("Careful. Quality will go down unfortunately. Cancel now if mp4 works for you.\nUse 'Restore MP4BAK.py' to restore your files afterwards.")
    t0 = time.perf_counter()

    RESET = "\033[0m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    DIM = "\033[2m"
    CYAN = "\033[36m"

    progress = {}
    progress_lock = threading.Lock()
    stop_display = threading.Event()
    cancel_event = threading.Event()
    completed_count = {"n": 0}
    log_lines: list[str] = []
    log_max = 8

    files = [
        p for p in directory.rglob("*.mp4")
    ]
    total_files = len(files)

    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 1) // 4)

    slot_pool: queue.Queue = queue.Queue()
    for i in range(max_workers):
        slot_pool.put(i)

    smoothed_rate = {"value": 0.0}
    def display():
        bar_width = 50
        print("\033[?25l", end="")  # hide cursor
        try:
            while not stop_display.is_set():
                with progress_lock:
                    items = dict(progress)
                    done = completed_count["n"]
                    log_snapshot = list(log_lines)

                elapsed = time.perf_counter() - t0
                in_flight_progress = sum(pct for _, pct in items.values())
                effective_done = done + in_flight_progress

                instant_rate = effective_done / elapsed if elapsed > 0 else 0
                alpha = 0.05  # lower = smoother/slower to react, higher = more responsive
                smoothed_rate["value"] = (
                    instant_rate if smoothed_rate["value"] == 0
                    else alpha * instant_rate + (1 - alpha) * smoothed_rate["value"]
                )

                eta = (total_files - effective_done) / smoothed_rate["value"] if smoothed_rate["value"] > 0 else 0
                eta_m = eta // 60
                eta_h = eta_m // 60
                eta = eta % 60
                eta_m = eta_m % 60
                eta_str = f"{eta_h:5.0f}h {eta_m:02.0f}m {eta:02.0f}s"

                overall_pct = effective_done / total_files if total_files else 1.0
                overall_filled = int(bar_width * overall_pct)
                overall_bar = "█" * overall_filled + "-" * (bar_width - overall_filled)

                frame = ["\033[H"]
                frame.append(f"{CYAN}Overall: [{overall_bar}] {overall_pct*100:5.1f}%  "
                              f"{done}/{total_files}  ETA {eta_str}{RESET}\033[K\n")
                frame.append("\033[K\n")

                for worker in range(max_workers):
                    if worker in items:
                        name, pct = items[worker]
                        filled = int(bar_width * pct)
                        bar = "█" * filled + "-" * (bar_width - filled)
                        frame.append(f"{worker:2d}: [{bar}] {pct*100:5.1f}% {name}\033[K\n")
                    else:
                        frame.append(f"{DIM}{worker:2d}: [{'-' * bar_width}]   idle{RESET}\033[K\n")

                frame.append("\033[K\n")
                frame.append(f"{DIM}Recent:{RESET}\033[K\n")
                for line in log_snapshot[-log_max:]:
                    frame.append(f"{line}\033[K\n")
                for _ in range(log_max - len(log_snapshot)):
                    frame.append("\033[K\n")

                frame.append("\033[J")
                print("".join(frame), end="", flush=True)
                time.sleep(0.2)
        finally:
            print("\033[?25h", end="", flush=True)  # show cursor again

    print("\033[2J", end="")
    threading.Thread(target=display, daemon=True).start()

    converted = 0

    print(f"Found {len(files)} file(s). Using {max_workers} worker(s).")
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    convert_mp4_to_webm,
                    path,
                    slot_pool,
                    progress,
                    progress_lock,
                    cancel_event,
                )
                for path in files
            ]

            try:
                for future in as_completed(futures):
                    try:
                        success, message = future.result()
                    except concurrent.futures.CancelledError:
                        continue
                    color = GREEN if success else RED
                    with progress_lock:
                        completed_count["n"] += 1
                        log_lines.append(f"{color}{message}{RESET}")
                    if success:
                        converted += 1
            except KeyboardInterrupt:
                cancel_event.set()
                for f in futures:
                    f.cancel()  # drops not-yet-started futures from the queue
                stop_display.set()
                print("\033[?25h", end="", flush=True)
                print("\nInterrupted. Waiting for running conversions to finish...")
                raise

    except KeyboardInterrupt:
        pass

    elapsed = time.perf_counter() - t0
    stop_display.set()
    time.sleep(0.25)
    print(f"\nDone. Converted {converted} file(s) in {elapsed:.1f} seconds.")


if __name__ == "__main__":
    process_directory()
