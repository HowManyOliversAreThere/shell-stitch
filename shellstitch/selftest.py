"""End-to-end check on synthetic photos: used to verify packaged builds.

Generates a synthetic section with every imperfection the engine corrects (camera tilt and
lens distortion, exposure differences, vignetting, dust on the optics, a duplicate and an
odd-magnification photo; see synthetic.py), stitches it with the real engine and checks the
right photos were placed accurately. With gui=True it also builds the main window off-screen.
Exit code 0 means success. The full test suite is in tests/.
"""

import os
import sys
import tempfile
import time


def run(gui=False):
    from .engine import Reporter, stitch_folder
    from .options import Options
    from .synthetic import make_section

    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="shellstitch-selftest-")
    log_path = os.path.join(tmp, "selftest.log")
    lines = []

    def say(msg):
        lines.append(msg)
        print(msg, flush=True)

    ok = False
    try:
        section = os.path.join(tmp, "synthetic")
        os.makedirs(section)
        truth = make_section(section, lens=True, gains=True, vignette=True, dust=True, copy=True,
                             odd_zoom=True)
        n = len(truth.photos)
        opts = Options(output=os.path.join(tmp, "out"), workers=2)
        rep = Reporter()
        rep.log = lambda msg, level="info": say(msg)
        res = stitch_folder(section, opts, rep)
        placed_ok = res["status"] == "done" and res["placed"] == n
        accurate = res["median_error_px"] < 1.0
        tif_ok = os.path.getsize(res["outputs"]["tif"]) > 0
        say(f"engine: placed {res['placed']}/{n}, error {res['median_error_px']:.2f}px, tif={tif_ok}")
        excluded_ok = [e["file"] for e in res["excluded"]] == truth.excluded
        say(f"engine: left out {[e['file'] for e in res['excluded']]} (expected {truth.excluded})")
        ok = placed_ok and accurate and tif_ok and excluded_ok
        if gui:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
            from .gui import theme
            from .gui.app import MainWindow
            app = QApplication.instance() or QApplication(sys.argv)
            theme.apply(app)
            win = MainWindow()
            win.results.set_output_dir(opts.output)
            win.results.refresh()
            shown = win.results.list.count()
            say(f"gui: main window built, results listed: {shown}")
            ok = ok and shown == 1
            win.deleteLater()
    except Exception as e:  # report any failure as a non-zero exit code
        import traceback
        say(traceback.format_exc())
        say(f"FAILED: {e}")
    say(f"self-test {'passed' if ok else 'FAILED'} in {time.time() - t0:.0f}s (files in {tmp})")
    with open(log_path, "w") as fh:
        fh.write("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(gui="--gui" in sys.argv))
