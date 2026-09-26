"""Shared test helpers: run the engine on a synthetic section and score it against the truth."""

import json
import os

import cv2
import numpy as np
import tifffile

from shellstitch.engine import Reporter, stitch_folder
from shellstitch.options import Options

# Options every test module uses unless it's testing that option.
FAST = {"workers": 2}

# Every option must be exercised by a test that checks its effect (see test_option_coverage).
COVERED = set()

# A valid non-default value for every option, used to check the command line and the GUI
# settings form pass each one through unchanged. test_option_coverage checks it's complete.
NONDEFAULT = {
    "exclude": ["*.bak", "scale*"],
    "perspective": False,
    "refine": False,
    "match_width": 1200,
    "features": 5000,
    "min_inliers": 12,
    "ransac_px": 3.5,
    "solve_time_limit": 45.0,
    "blend": "seam",
    "feather_power": 6.5,
    "gain": False,
    "flat": False,
    "output": "some/other/folder",
    "preview_width": 2500,
    "preview_only": True,
    "skip_existing": True,
    "workers": 3,
}


def covers(*names):
    """Mark a test as checking the given Options fields."""
    COVERED.update(names)
    return lambda fn: fn


class Recorder(Reporter):
    """Collects log lines and the stages that ran, instead of printing them."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.stages = []

    def log(self, msg, level="info"):
        self.lines.append((level, msg.strip()))

    def busy(self, desc):
        self.stages.append(desc)
        super().busy(desc)

    def track(self, iterable, total, desc):
        self.stages.append(desc)
        yield from super().track(iterable, total, desc)

    def text(self):
        return "\n".join(m for _, m in self.lines)


def stitch(truth, out_dir, **opts):
    """Stitch a synthetic section; returns (result, report, mosaic BGR or None, log recorder)."""
    options = Options(output=str(out_dir), **{**FAST, **opts})
    rec = Recorder()
    result = stitch_folder(truth.folder, options, rec)
    report = mosaic = None
    if result["status"] in ("done", "preview"):
        with open(result["outputs"]["report"]) as fh:
            report = json.load(fh)
        if os.path.exists(result["outputs"]["tif"]):
            mosaic = tifffile.imread(result["outputs"]["tif"])[..., ::-1]  # RGB -> BGR
    return result, report, mosaic, rec


def rigid_fit(src, dst):
    """Rotation + translation (no scale) that best maps src points onto dst points."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    cs, cd = src.mean(0), dst.mean(0)
    U, _, Vt = np.linalg.svd((src - cs).T @ (dst - cd))
    R = (U @ Vt).T
    if np.linalg.det(R) < 0:
        Vt[-1] *= -1
        R = (U @ Vt).T
    return R, cd - R @ cs


def evaluate(truth, report, mosaic=None):
    """Scores a stitch against the ground truth.

    pos_err: worst photo-centre error (px) after the best rigid fit of mosaic to scene
    angle_err: worst photo-rotation error (degrees), relative to the same fit
    coverage: mosaic pixels covered / true footprint of the photos (1.0 is ideal)
    mae: mean absolute pixel difference between mosaic and true scene (0-255 scale), after
         allowing for an overall per-channel brightness factor
    """
    truth_by_file = {p["file"]: p for p in truth.photos}
    placed = [i for i in report["images"] if i["file"] in truth_by_file]
    src = [i["centre_xy"] for i in placed]
    dst = [truth_by_file[i["file"]]["centre"] for i in placed]
    R, t = rigid_fit(src, dst)
    pos_err = float(np.max(np.linalg.norm(np.asarray(src) @ R.T + t - np.asarray(dst), axis=1)))
    fit_angle = np.degrees(np.arctan2(R[1, 0], R[0, 0]))
    angle_err = max(abs(((i["rotation_deg"] + fit_angle) - truth_by_file[i["file"]]["angle_deg"] + 180) % 360 - 180)
                    for i in placed)
    scores = {"pos_err": pos_err, "angle_err": float(angle_err)}
    if mosaic is not None:
        h, w = mosaic.shape[:2]
        A = np.hstack([R, t[:, None]]).astype(np.float64)  # mosaic px -> scene px
        flags = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
        expected = cv2.warpAffine(truth.scene, A, (w, h), flags=flags).astype(np.float64)
        in_scene = cv2.warpAffine(np.ones(truth.scene.shape[:2], np.uint8), A, (w, h), flags=flags) > 0
        covered = mosaic.max(2) > 0
        scores["coverage"] = covered.sum() / truth.footprint_area()
        use = covered & in_scene
        use = cv2.erode(use.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0  # ignore edge interpolation
        m, s = mosaic[use].astype(np.float64), expected[use]
        k = (m * s).sum(0) / (s * s).sum(0)
        scores["mae"] = float(np.abs(m - k * s).mean())
    return scores
