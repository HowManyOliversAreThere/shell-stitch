"""Runs the stitching engine on a background thread and reports progress to the UI."""

import time
import traceback

from PySide6.QtCore import QObject, QThread, Signal

from ..engine import Cancelled, Reporter, SkipSection, stitch_folder


class QtReporter(Reporter):
    """Forwards engine progress as Qt signals (delivered safely on the UI thread)."""

    def __init__(self, signals):
        super().__init__()
        self.s = signals
        self._last_tick = 0.0

    def log(self, msg, level="info"):
        self.s.log.emit(msg.strip(), level)

    def busy(self, desc):
        self.check()
        self.s.stage.emit(desc, 0)

    def tick(self, detail):
        self.check()
        now = time.monotonic()
        if now - self._last_tick > 0.2:  # don't flood the UI with updates
            self.s.detail.emit(detail)
            self._last_tick = now

    def track(self, iterable, total, desc):
        self.check()
        self.s.stage.emit(desc, total)
        done, last = 0, 0.0
        for item in iterable:
            self.check()
            yield item
            done += 1
            now = time.monotonic()
            if now - last > 0.05 or done == total:  # don't flood the UI with updates
                self.s.advance.emit(done)
                last = now
        self.check()


class StitchWorker(QObject):
    log = Signal(str, str)                 # message, level
    stage = Signal(str, int)               # description, total steps (0 = busy)
    advance = Signal(int)                  # steps done in the current stage
    detail = Signal(str)                   # live status within a busy stage
    sectionStarted = Signal(int)           # index into the job list
    sectionFinished = Signal(int, dict)    # index, result
    finished = Signal(bool)                # True if cancelled

    def __init__(self, jobs, opts):
        super().__init__()
        self.jobs = jobs  # [(name, folder path)]
        self.opts = opts
        self.reporter = QtReporter(self)

    def cancel(self):
        self.reporter.cancel()

    def skip(self):
        """Abandon the current section and continue with the next."""
        self.reporter.skip()

    def run(self):
        cancelled = False
        for idx, (name, path) in enumerate(self.jobs):
            if self.reporter.cancelled:
                cancelled = True
                break
            self.sectionStarted.emit(idx)
            try:
                result = stitch_folder(path, self.opts, self.reporter)
            except SkipSection:
                self.log.emit(f"[{name}] skipped", "warning")
                self.sectionFinished.emit(idx, {"name": name, "status": "skipped", "message": "by you"})
                continue
            except Cancelled:
                self.log.emit(f"[{name}] cancelled", "warning")
                self.sectionFinished.emit(idx, {"name": name, "status": "cancelled"})
                cancelled = True
                break
            except Exception as e:  # report and carry on with the next section
                self.log.emit(f"[{name}] FAILED: {e}", "error")
                self.log.emit(traceback.format_exc(), "debug")
                self.sectionFinished.emit(idx, {"name": name, "status": "failed", "message": str(e)})
                continue
            self.sectionFinished.emit(idx, result)
        self.finished.emit(cancelled)


def start(worker):
    """Run `worker` on a new QThread; returns the thread (keep a reference).

    The owner stops it with thread.quit(); thread.wait() once `worker.finished` arrives.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    return thread
