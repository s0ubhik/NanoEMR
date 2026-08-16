import os
import signal
import subprocess
import sys
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BASE_DIR = "./"
RUN_FILE = os.path.join(BASE_DIR, "run.py")

process = None
last_restart = 0
RESTART_DELAY = 1.5


def start():
    global process

    print("Starting NanoEMR...")

    process = subprocess.Popen(
        ["python3", "run.py"],
        cwd=BASE_DIR
    )


def restart():
    global process, last_restart

    now = time.time()

    # Prevent restart loops
    if now - last_restart < RESTART_DELAY:
        return

    last_restart = now

    print("Change detected. Restarting...")

    if process and process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    time.sleep(0.5)
    start()


class Handler(FileSystemEventHandler):

    def should_ignore(self, path):
        path = os.path.abspath(path)

        # Ignore run.py to prevent self-triggering
        if path == RUN_FILE:
            return True

        # Ignore Python/cache/temp files
        ignored = [
            "__pycache__",
            "data",
            ".git",
            ".pyc",
            ".pyo",
            ".swp",
            ".swo",
            "~",
        ]

        return any(x in path for x in ignored)

    def on_modified(self, event):
        if event.is_directory:
            return

        if self.should_ignore(event.src_path):
            return

        print(f"File changed: {event.src_path}")
        restart()

    def on_created(self, event):
        if event.is_directory:
            return

        if self.should_ignore(event.src_path):
            return

        print(f"File created: {event.src_path}")
        restart()

    def on_deleted(self, event):
        if event.is_directory:
            return

        if self.should_ignore(event.src_path):
            return

        print(f"File deleted: {event.src_path}")
        restart()


if __name__ == "__main__":

    start()

    observer = Observer()
    observer.schedule(
        Handler(),
        BASE_DIR,
        recursive=True
    )

    observer.start()

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        observer.stop()

        if process and process.poll() is None:
            process.terminate()

    observer.join()
