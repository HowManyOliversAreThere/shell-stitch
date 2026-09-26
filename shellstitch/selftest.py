"""End-to-end check on synthetic photos: used to verify packaged builds.

Makes a textured "shell section", cuts overlapping, slightly rotated photos out of it, stitches
them with the real engine and checks every photo was placed accurately. With gui=True it also
builds the main window off-screen. Exit code 0 means success.
"""

import os
import sys
import tempfile
import time

import cv2
import numpy as np


def make_photos(folder, rows=2, cols=3, w=640, h=480, overlap=0.35, seed=0):
    rng = np.random.default_rng(seed)
    step_x, step_y = int(w * (1 - overlap)), int(h * (1 - overlap))
    W, H = w + step_x * (cols - 1) + 200, h + step_y * (rows - 1) + 200
    # growth-line-like bands plus fine grain
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    bands = 40 * np.sin(y / 9 + 3 * np.sin(x / 150)) + 25 * np.sin(y / 31 + x / 400)
    grain = cv2.GaussianBlur(rng.normal(0, 40, (H, W)).astype(np.float32), (0, 0), 1.5)
    base = np.clip(128 + bands + grain, 0, 255).astype(np.uint8)
    scene = cv2.merge([base, (base * 0.95).astype(np.uint8), (base * 0.9).astype(np.uint8)])
    n = 0
    for r in range(rows):
        for c in range(cols):
            cx, cy = 100 + c * step_x + w / 2, 100 + r * step_y + h / 2
            M = cv2.getRotationMatrix2D((cx, cy), float(rng.uniform(-3, 3)), 1.0)
            M[:, 2] += [w / 2 - cx, h / 2 - cy]
            photo = cv2.warpAffine(scene, M, (w, h), flags=cv2.INTER_LINEAR)
            cv2.imwrite(os.path.join(folder, f"image{n:04d}.tif"), photo)
            n += 1
    return n


def run(gui=False):
    from .engine import Reporter, stitch_folder
    from .options import Options

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
        n = make_photos(section)
        opts = Options(output=os.path.join(tmp, "out"), workers=2)
        rep = Reporter()
        rep.log = lambda msg, level="info": say(msg)
        res = stitch_folder(section, opts, rep)
        placed_ok = res["status"] == "done" and res["placed"] == n
        accurate = res["median_error_px"] < 1.0
        tif_ok = os.path.getsize(res["outputs"]["tif"]) > 0
        say(f"engine: placed {res['placed']}/{n}, error {res['median_error_px']:.2f}px, tif={tif_ok}")
        ok = placed_ok and accurate and tif_ok
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
