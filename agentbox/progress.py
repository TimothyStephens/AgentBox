"""Progress lines for the steps of a session's start and end that can take a while (a backup of a big tree,
the grant walks, the git-history read, the end-of-session scan).

A step that finishes within `DELAY` prints nothing, so a fast session looks as it always did. One that runs
longer says what it is doing, then repeats a line every `HEARTBEAT` seconds with the elapsed time (and a
percentage when the step can measure one), and ends with how long it took. Whole lines only, never a
redrawn line, so the output reads the same in a terminal, a log or a captured run, and a warning printed
by the step itself cannot collide with it. The cost is one sleeping thread per step.
"""
import threading
import time

DELAY = 2.0
HEARTBEAT = 15.0


def elapsed(seconds):
    s = int(seconds)
    return f"{seconds:.1f}s" if seconds < 60 else f"{s // 60}m{s % 60:02d}s"


class Step:
    """`with Step("backing up /x", out) as st: ... st.progress(0.4, "12,000 of 30,000 files")`.

    `out` takes one line (the session's `warn`). `progress` may be called from the step's own thread as often
    as it likes: it only stores the latest value, which the next heartbeat prints."""

    def __init__(self, label, out, *, delay=None, heartbeat=None, clock=time.monotonic):
        self.label, self.out, self.clock = label, out, clock
        self.delay = DELAY if delay is None else delay
        self.heartbeat = HEARTBEAT if heartbeat is None else heartbeat
        self._frac = self._detail = None
        self._announced = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def progress(self, frac=None, detail=None):
        self._frac, self._detail = frac, detail

    def _state(self):
        bits = [elapsed(self.clock() - self.t0)]
        if self._frac is not None:
            bits.append(f"{min(max(self._frac, 0.0), 1.0):.0%}")
        if self._detail:
            bits.append(self._detail)
        return ", ".join(bits)

    def _say(self, line):
        with self._lock:
            if not self._stop.is_set():
                self._announced = True
                self.out(line)

    def _run(self):
        if self.delay > 0:
            if self._stop.wait(self.delay):
                return
            self._say(f"agentbox: {self.label} ...")
        while not self._stop.wait(self.heartbeat):
            self._say(f"agentbox: {self.label}: {self._state()}")

    def __enter__(self):
        self.t0 = self.clock()
        if self.delay <= 0:
            self._say(f"agentbox: {self.label} ...")     # at once, so a zero delay always shows the step
        self._thread = threading.Thread(target=self._run, name="ab-progress", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        with self._lock:
            self._stop.set()
            announced = self._announced
        self._thread.join()
        if announced:
            took = elapsed(self.clock() - self.t0)
            self.out(f"agentbox: {self.label}: done in {took}" if exc_type is None
                     else f"agentbox: {self.label}: stopped after {took}")
        return False
