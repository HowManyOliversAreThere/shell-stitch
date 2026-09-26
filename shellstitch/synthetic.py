"""Synthetic shell sections with a known ground truth, for tests and the packaged self-test.

A procedurally generated "shell" scene (growth bands, grain, colour variation) is
photographed as a grid of overlapping, slightly rotated photos through the same camera model
the engine uses. Optional imperfections mimic real captures so each stitching setting has
something to correct:

- `lens`: camera tilt (perspective), aspect and radial distortion shared by all photos
- `gains`: exposure / white-balance differences between photos
- `vignette`: a lighting falloff shared by all photos
- `dust`: dark specks fixed to the camera (the same place in every photo)
- `copy`: an "image - Copy.tif" duplicate with a burned-in scale bar (skipped by default)
- `odd_zoom`: one extra photo taken at a different magnification (should be left out)

Everything is generated from a seed, so datasets are reproducible and license-free.
"""

import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from .engine import Model

DEFAULT_LENS = (0.004, 0.002, 0.012, -0.006, -0.02)  # aspect, shear, persp x, persp y, radial k1


@dataclass
class Truth:
    """What a correct stitch of a synthetic section should find."""

    folder: str
    scene: np.ndarray                      # BGR uint8
    photo_size: tuple                      # (w, h)
    photos: list = field(default_factory=list)   # [{file, centre: (x, y) scene px, angle_deg}]
    lens: tuple = (0.0, 0.0, 0.0, 0.0, 0.0)
    um_per_px: float | None = None
    excluded: list = field(default_factory=list)  # files a correct stitch leaves out
    skipped_by_default: list = field(default_factory=list)  # files the default exclude skips

    def footprint_area(self):
        """Area (scene px) covered by the union of the photos: what the mosaic should cover."""
        mask = np.zeros(self.scene.shape[:2], np.uint8)
        for p in self.photos:
            outline = photo_outline(p, self.photo_size, self.lens)
            cv2.fillPoly(mask, [np.round(outline).astype(np.int32)], 1)
        return int(mask.sum())


def make_scene(w, h, seed):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    bands = 38 * np.sin(y / 9 + 3 * np.sin(x / 150)) + 22 * np.sin(y / 31 + x / 400)
    grain = cv2.GaussianBlur(rng.normal(0, 40, (h, w)).astype(np.float32), (0, 0), 1.5)
    blobs = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 25)
    base = 128 + bands + grain + 400 * blobs
    tint = 0.12 * np.sin(x / 300)[..., None] * np.array([1.0, 0.0, -1.0], np.float32)
    scene = base[..., None] * (np.array([1.0, 0.95, 0.88], np.float32) + tint)
    return np.clip(scene, 0, 255).astype(np.uint8)


def photo_maps(photo, size, lens):
    """Scene coordinates of every pixel of a photo (the engine's camera model)."""
    w, h = size
    cw = w / 2
    c = np.array([(w - 1) / 2, (h - 1) / 2])
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    th = np.radians(photo["angle_deg"])
    tx, ty = np.asarray(photo["centre"]) / cw
    X, Y = Model.forward_arrays(np.asarray(lens, float), th, tx, ty, (xs - c[0]) / cw, (ys - c[1]) / cw)
    return (X * cw).astype(np.float32), (Y * cw).astype(np.float32)


def photo_outline(photo, size, lens, n=12):
    w, h = size
    t = np.linspace(0, 1, n, endpoint=False)
    pts = np.concatenate([np.c_[t * (w - 1), 0 * t], np.c_[(w - 1) + 0 * t, t * (h - 1)],
                          np.c_[(w - 1) * (1 - t), (h - 1) + 0 * t], np.c_[0 * t, (h - 1) * (1 - t)]])
    mx, my = photo_maps(photo, size, lens)
    ix, iy = np.clip(np.round(pts).astype(int), 0, [w - 1, h - 1]).T
    return np.c_[mx[iy, ix], my[iy, ix]]


def write_calibration(path, um_per_px, width, height):
    """A minimal Leica LAS .eax sidecar, as the engine reads it."""
    meta = os.path.join(os.path.dirname(path), ".Metadata")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, os.path.basename(path) + ".eax"), "w", encoding="utf-8") as fh:
        fh.write(f"""<?xml version="1.0"?>
<DXExtendedAnnotationSettings xmlns="http://www.leica.com">
  <ImageSize><Width>{width}</Width><Height>{height}</Height></ImageSize>
  <Calibration><Units>mm</Units><MetresPerPixel>{um_per_px * 1e-6!r}</MetresPerPixel></Calibration>
</DXExtendedAnnotationSettings>
""")


def make_section(folder, rows=2, cols=3, size=(400, 300), overlap=0.38, seed=0, rotation=4.0,
                 lens=False, gains=False, vignette=False, gradient=False, dust=False, copy=False, odd_zoom=False,
                 um_per_px=10.0):
    """Writes a synthetic section's photos to `folder` and returns its Truth."""
    os.makedirs(folder, exist_ok=True)
    rng = np.random.default_rng(seed)
    w, h = size
    lens_params = DEFAULT_LENS if lens else (0.0, 0.0, 0.0, 0.0, 0.0)
    step_x, step_y = w * (1 - overlap), h * (1 - overlap)
    margin = 0.35 * max(w, h)
    scene_w = int(w + step_x * (cols - 1) + 2 * margin)
    scene_h = int(h + step_y * (rows - 1) + 2 * margin)
    scene = make_scene(scene_w, scene_h, seed)
    truth = Truth(folder, scene, (w, h), lens=lens_params, um_per_px=um_per_px)

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    r2 = ((xs - w / 2) ** 2 + (ys - h / 2) ** 2) / ((w / 2) ** 2 + (h / 2) ** 2)
    shading = (1 - 0.3 * r2) if vignette else np.ones((h, w), np.float32)
    if gradient:  # light from one side
        shading = shading * (1 - 0.3 * xs / w)
    specks = np.ones((h, w), np.float32)
    if dust:
        for _ in range(6):
            sx, sy = rng.uniform(0.1, 0.9) * w, rng.uniform(0.1, 0.9) * h
            specks *= 1 - 0.6 * np.exp(-((xs - sx) ** 2 + (ys - sy) ** 2) / (2 * 3.0 ** 2))

    def shoot(photo, name, zoom=1.0):
        mx, my = photo_maps(photo, size, lens_params)
        if zoom != 1.0:
            cx, cy = photo["centre"]
            mx, my = cx + (mx - cx) / zoom, cy + (my - cy) / zoom
        im = cv2.remap(scene, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT).astype(np.float32)
        g = photo.get("gain", (1.0, 1.0, 1.0))
        im = im * (shading * specks)[..., None] * np.asarray(g, np.float32)
        path = os.path.join(folder, name)
        cv2.imwrite(path, np.clip(im + 0.5, 0, 255).astype(np.uint8))
        if um_per_px:
            write_calibration(path, um_per_px / zoom, w, h)
        return path

    n = 0
    for r in range(rows):
        for c in range(cols):
            photo = {
                "file": f"image{n:04d}.tif",
                "centre": (margin + w / 2 + c * step_x + rng.uniform(-8, 8),
                           margin + h / 2 + r * step_y + rng.uniform(-8, 8)),
                "angle_deg": float(rng.uniform(-rotation, rotation)) + (2 * rotation if r % 2 else 0.0),
            }
            if gains:
                base = rng.uniform(0.8, 1.2)
                photo["gain"] = tuple(base * rng.uniform(0.95, 1.05, 3))
            shoot(photo, photo["file"])
            truth.photos.append(photo)
            n += 1

    if copy:  # duplicate of the first photo with a burned-in scale bar, as LAS exports it
        im = cv2.imread(os.path.join(folder, truth.photos[0]["file"]))
        cv2.rectangle(im, (w - 90, h - 14), (w - 20, h - 10), (40, 40, 200), -1)
        name = "image - Copy.tif"
        cv2.imwrite(os.path.join(folder, name), im)
        if um_per_px:
            write_calibration(os.path.join(folder, name), um_per_px, w, h)
        truth.skipped_by_default.append(name)

    if odd_zoom:  # an extra photo at 1.25x magnification, overlapping the first
        p = dict(truth.photos[0], file=f"image{n:04d}.tif")
        shoot(p, p["file"], zoom=1.25)
        truth.excluded.append(p["file"])
    return truth
