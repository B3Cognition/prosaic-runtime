"""Status-only stderr progress, including during silent model reasoning."""
import sys
import threading
import time


class Progress:
    def __init__(self, quiet=False, interval=5):
        self.quiet = quiet
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.interval = interval

    def __enter__(self):
        def heartbeat():
            while not self.stop.wait(self.interval):
                self.write(f"working ({time.monotonic() - self.started:.0f}s elapsed)")
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        if not self.quiet:
            self.thread.start()
        return self

    def write(self, text):
        if not self.quiet:
            print(f"[prosaic-runtime] {text}", file=sys.stderr, flush=True)

    def __call__(self, event):
        kind = event["event"]
        if kind in {"started", "completed"}:
            self.write(kind)
        elif kind in {"tool_started", "tool_completed"}:
            self.write(f"{kind}: {event['name']}" + (f" ({event['status']}, {event['duration_ms']}ms)" if kind == "tool_completed" else ""))

    def __exit__(self, *args):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
