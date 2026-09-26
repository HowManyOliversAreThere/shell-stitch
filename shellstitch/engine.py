"""Stitching engine: overlapping microscope photos of a shell section -> one mosaic.

Each input folder holds overlapping close-ups of one section, captured in any order or
scan pattern. Every image is matched against every other image with SIFT features, then all
matches are combined in one global least-squares adjustment. The model is physical: the
sample moves rigidly under a fixed camera (rotation + translation per photo, no scaling),
and the camera's slight oblique view and lens distortion are shared by every photo and
solved for at the same time. Exposure and illumination differences are evened out and the
photos are blended onto one canvas, written as an uncompressed TIFF plus a JPEG preview.

Progress, logging and cancellation go through a `Reporter`, so the same engine drives both
the command line and the GUI.
"""

import fnmatch
import json
import math
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cv2
import numpy as np
import tifffile
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from . import __version__
from .options import Options

IMAGE_EXTS = (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp")
N_LENS = 5  # shared camera parameters: aspect, shear, 2x perspective, radial distortion


class Cancelled(Exception):
    """Raised inside the engine when the user asks to stop."""


class SkipSection(Cancelled):
    """Raised inside the engine when the user asks to skip the current section only."""


class Reporter:
    """Receives progress from the engine. The base class prints plain log lines.

    Subclasses (command line, GUI) override `track`, `busy`, `tick` and `log`. `cancel()` and
    `skip()` may be called from any thread and take effect at the next progress step.
    """

    def __init__(self):
        self._cancel = threading.Event()
        self._skip = threading.Event()

    def cancel(self):
        self._cancel.set()

    def skip(self):
        """Abandon the current section and carry on with the next one."""
        self._skip.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()
        if self._skip.is_set():
            self._skip.clear()
            raise SkipSection()

    def log(self, msg, level="info"):
        print(msg, flush=True)

    def section(self, name):
        """A new folder is being stitched."""
        self._skip.clear()

    def busy(self, desc):
        """A stage without measurable progress has started."""
        self.check()

    def tick(self, detail):
        """Progress within a `busy` stage (e.g. solver iterations), to show it is still working."""
        self.check()

    def track(self, iterable, total, desc):
        """Wrap a stage's work items; yields them while reporting progress."""
        for item in iterable:
            self.check()
            yield item
        self.check()


def pmap(rep, fn, items, desc, workers):
    """Parallel map in item order, with progress, that stops promptly when cancelled."""
    items = list(items)
    ex = ThreadPoolExecutor(max(1, workers))
    try:
        return list(rep.track(ex.map(fn, items), len(items), desc))
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


# ----------------------------------------------------------------------------- inputs

def list_images(folder, exclude):
    files = []
    for f in sorted(os.listdir(folder)):
        p = os.path.join(folder, f)
        if f.startswith(".") or not os.path.isfile(p) or not f.lower().endswith(IMAGE_EXTS):
            continue
        if any(fnmatch.fnmatch(f, pat) for pat in exclude):
            continue
        files.append(p)
    return files


def leica_um_per_px(path, width):
    """Micrometres per pixel from the calibration LAS writes to .Metadata/<name>.eax.

    LAS records the calibration for the image size stated in the same file, which is not
    always the size the image was saved at (e.g. 1024 px calibration on a 3648 px capture),
    so it is rescaled to the actual width.
    """
    eax = os.path.join(os.path.dirname(path), ".Metadata", os.path.basename(path) + ".eax")
    try:
        with open(eax, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"<MetresPerPixel>([^<]+)</MetresPerPixel>", text)
    if not m:
        return None
    um = float(m.group(1)) * 1e6
    w = re.search(r"<ImageSize>\s*<Width>(\d+)</Width>", text)
    if w and int(w.group(1)) > 0:
        um *= int(w.group(1)) / width
    return um


def read_image(path):
    im = cv2.imread(path, cv2.IMREAD_COLOR)
    if im is None:
        raise RuntimeError(f"could not read {path}")
    return im


def image_size(path):
    """(width, height) without decoding the whole image where possible."""
    try:
        with tifffile.TiffFile(path) as t:
            p = t.pages[0]
            return p.imagewidth, p.imagelength
    except Exception:
        im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        return (im.shape[1], im.shape[0]) if im is not None else None


def output_paths(output_dir, name):
    base = os.path.join(output_dir, name)
    return {"tif": base + ".tif", "preview": base + "_preview.jpg",
            "layout": base + "_layout.jpg", "report": base + "_report.json"}


def find_sections(root, exclude=(), output_dir=None):
    """Folders under `root` (and root itself) that hold at least two photos.

    Returns [{name, path, n_images, image_size}] sorted by name. The output folder is left out.
    """
    out = os.path.realpath(output_dir) if output_dir else None
    cands = [root] + [os.path.join(root, d) for d in sorted(os.listdir(root))
                      if not d.startswith(".") and os.path.isdir(os.path.join(root, d))]
    found = []
    for d in cands:
        if out and os.path.realpath(d) == out:
            continue
        try:
            imgs = list_images(d, exclude)
        except OSError:
            continue
        if len(imgs) < 2:
            continue
        found.append({"name": os.path.basename(os.path.normpath(d)), "path": d,
                      "n_images": len(imgs), "image_size": image_size(imgs[0])})
    return found


# ----------------------------------------------------------------------------- matching

def extract_features(path, match_width, n_features):
    im = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    h, w = im.shape
    s = min(1.0, match_width / w)
    small = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else im
    # CLAHE brings out the faint growth-line texture so plain-looking areas still match
    small = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(small)
    # low contrast threshold: the shell interior is smooth and needs faint features to match
    kps, des = cv2.SIFT_create(nfeatures=n_features, contrastThreshold=0.01).detectAndCompute(small, None)
    if des is None:
        return np.zeros((0, 2), np.float32), np.zeros((0, 128), np.float32), (w, h)
    des = np.sqrt(des / (des.sum(1, keepdims=True) + 1e-7)).astype(np.float32)  # RootSIFT
    pts = (np.float32([k.pt for k in kps]) + 0.5) / s - 0.5  # full-resolution pixels
    return pts, des, (w, h)


def raw_matches(fa, fb, min_inliers, ratio=0.85):
    """Descriptor matches between two photos (ratio test only): (points in a, points in b)."""
    pa, da, _ = fa
    pb, db, _ = fb
    empty = (np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32))
    if len(da) < 8 or len(db) < 8:
        return empty
    matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
    knn = matcher.knnMatch(da, db, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < ratio * m[1].distance]
    if len(good) < min_inliers:
        return empty
    return pa[[g.queryIdx for g in good]], pb[[g.trainIdx for g in good]]


class CameraFixed:
    """Finds features fixed to the camera rather than the sample.

    Dust or scratches on the optics or stage glass, or a light source's glare, stay at the same
    place in every photo, so they "match" at zero offset between photos that were taken at
    different positions and drag the alignment towards the wrong answer. A location counts as
    camera-fixed when zero-offset matches recur there across several photo pairs. Photos that
    were genuinely retaken at the same position also match at zero offset, but only with each
    other, so their matches are kept.
    """

    CELL = 8          # px grid for counting locations
    MIN_PAIRS = 3     # a location seen static in this many pairs is camera-fixed

    def __init__(self, raw, size):
        w, h = size
        self.tol = max(3.0, 0.01 * w)
        self.shape = (h // self.CELL + 1, w // self.CELL + 1)
        counts = np.zeros(self.shape, np.int32)
        for p, q in raw:
            cells = self._cells(p[self.static(p, q)])
            if len(cells):
                counts.flat[np.unique(cells)] += 1
        # a speck spans neighbouring cells, so count a 3x3 neighbourhood
        counts = cv2.dilate(counts.astype(np.float32), np.ones((3, 3), np.uint8))
        self.fixed = counts >= self.MIN_PAIRS

    def static(self, p, q):
        return np.hypot(*(p - q).T) <= self.tol

    def _cells(self, pts):
        c = np.clip((pts // self.CELL).astype(np.int64), 0, [self.shape[1] - 1, self.shape[0] - 1])
        return c[:, 1] * self.shape[1] + c[:, 0]

    def keep(self, p, q):
        """Mask of matches to keep (drops static matches at camera-fixed locations)."""
        drop = self.static(p, q)
        drop[drop] = self.fixed.flat[self._cells(p[drop])]
        return ~drop


def fit_pair(p, q, min_inliers, ransac_px):
    """Consistent rigid-ish fit to matches: (inlier p, inlier q, rotation, translation) or None."""
    if len(p) < min_inliers:
        return None
    M, inl = cv2.estimateAffinePartial2D(q, p, method=cv2.RANSAC,
                                         ransacReprojThreshold=ransac_px, maxIters=10000)
    if M is None:
        return None
    inl = inl.ravel().astype(bool)
    scale = math.hypot(M[0, 0], M[1, 0])
    if inl.sum() < min_inliers or not 0.95 < scale < 1.05:
        return None
    return p[inl], q[inl], math.atan2(M[1, 0], M[0, 0]), M[:, 2] / scale


# ----------------------------------------------------------------------------- camera model

def rot(theta):
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


class Model:
    """Maps image pixels to mosaic pixels.

    image px -> centred, normalised coords -> radial distortion -> perspective/aspect
    (shared by all photos) -> rigid pose of the photo (rotation + translation).
    Mosaic units are image pixels at the centre of the field of view.
    """

    def __init__(self, size, lens, poses):
        self.w, self.h = size
        self.cw = self.w / 2
        self.c = np.array([(self.w - 1) / 2, (self.h - 1) / 2])
        self.lens = np.asarray(lens, float)
        self.poses = poses  # k -> (theta, tx, ty) in normalised units
        a, b, g, h, _ = self.lens
        self.L = np.array([[1 + a, b, 0], [0, 1 / (1 + a), 0], [g, h, 1]])
        self.Linv = np.linalg.inv(self.L)

    @staticmethod
    def forward_arrays(lens, th, tx, ty, xn, yn):
        """Vectorised normalised forward map; used by the solver as well."""
        a, b, g, h, k1 = lens
        f = 1 + k1 * (xn * xn + yn * yn)
        xn, yn = xn * f, yn * f
        den = 1 + g * xn + h * yn
        u = ((1 + a) * xn + b * yn) / den
        v = (yn / (1 + a)) / den
        c, s = np.cos(th), np.sin(th)
        return c * u - s * v + tx, s * u + c * v + ty

    def to_mosaic(self, k, pts):
        th, tx, ty = self.poses[k]
        n = (np.asarray(pts, float) - self.c) / self.cw
        X, Y = self.forward_arrays(self.lens, th, tx, ty, n[:, 0], n[:, 1])
        return np.stack([X, Y], 1) * self.cw

    def from_mosaic(self, k, X, Y):
        """Inverse map for pixel grids (X, Y arrays in mosaic px) -> image px (float32)."""
        th, tx, ty = self.poses[k]
        c, s = math.cos(th), math.sin(th)
        X = X / self.cw - tx
        Y = Y / self.cw - ty
        u, v = c * X + s * Y, -s * X + c * Y
        Li = self.Linv
        den = Li[2, 0] * u + Li[2, 1] * v + Li[2, 2]
        xu = (Li[0, 0] * u + Li[0, 1] * v + Li[0, 2]) / den
        yu = (Li[1, 0] * u + Li[1, 1] * v + Li[1, 2]) / den
        k1 = self.lens[4]
        xd, yd = xu, yu
        for _ in range(4):  # invert radial distortion by fixed-point iteration
            f = 1 + k1 * (xd * xd + yd * yd)
            xd, yd = xu / f, yu / f
        return ((xd * self.cw + self.c[0]).astype(np.float32),
                (yd * self.cw + self.c[1]).astype(np.float32))

    def outline(self, k, n=16):
        """Image border mapped into the mosaic (n points per side)."""
        t = np.linspace(0, 1, n, endpoint=False)
        w, h = self.w - 1, self.h - 1
        pts = np.concatenate([np.c_[t * w, 0 * t], np.c_[w + 0 * t, t * h],
                              np.c_[w - t * w, h + 0 * t], np.c_[0 * t, h - t * h]])
        return self.to_mosaic(k, pts)

    def rotation_deg(self, k):
        return math.degrees(self.poses[k][0])


# ----------------------------------------------------------------------------- global alignment

def initial_poses(edges, ref):
    """Chain pairwise rigid fits along a maximum spanning tree (most matches first).

    Returns k -> (theta, T) with world = R(theta) x + T in pixel coordinates.
    """
    poses = {ref: (0.0, np.zeros(2))}
    adj = {}
    for e in edges:
        adj.setdefault(e["i"], []).append(e)
        adj.setdefault(e["j"], []).append(e)
    frontier = list(adj.get(ref, []))
    while frontier:
        frontier.sort(key=lambda e: len(e["p"]))
        e = frontier.pop()
        i, j = e["i"], e["j"]
        if (i in poses) == (j in poses):
            continue
        # e maps points of j into the frame of i:  x_i = R(th) x_j + t
        if i in poses:
            ti, Ti = poses[i]
            new, pose = j, (ti + e["theta"], Ti + rot(ti) @ e["t"])
        else:
            tj, Tj = poses[j]
            th = tj - e["theta"]
            new, pose = i, (th, Tj - rot(th) @ e["t"])
        poses[new] = pose
        frontier.extend(adj[new])
    return poses


def solve_global(edges, ref, size, fit_lens, max_pts=200, start=None, rep=None, time_limit=None):
    """Robust least squares over all matches; returns the Model and per-edge median residual.

    Reports each solver iteration through `rep.tick` and stops early (keeping the best solution
    so far) after `time_limit` seconds.
    """
    init = initial_poses(edges, ref)
    comp = sorted(init)
    free = [k for k in comp if k != ref]
    idx = {k: m for m, k in enumerate(free)}
    edges = [e for e in edges if e["i"] in init and e["j"] in init]
    nl = N_LENS if fit_lens else 0
    cw = size[0] / 2
    c = np.array([(size[0] - 1) / 2, (size[1] - 1) / 2])

    rng = np.random.default_rng(0)
    I, J, P, Q, counts = [], [], [], [], []
    for e in edges:
        sel = rng.choice(len(e["p"]), min(len(e["p"]), max_pts), replace=False)
        I.append(np.full(len(sel), e["i"]))
        J.append(np.full(len(sel), e["j"]))
        P.append((e["p"][sel] - c) / cw)
        Q.append((e["q"][sel] - c) / cw)
        counts.append(len(sel))
    I, J, P, Q = np.concatenate(I), np.concatenate(J), np.concatenate(P), np.concatenate(Q)

    x0 = np.zeros(nl + 3 * len(free))
    for k, m in idx.items():
        if start is not None and k in start.poses:
            x0[nl + 3 * m:nl + 3 * m + 3] = start.poses[k]
        else:
            th, T = init[k]
            x0[nl + 3 * m] = th
            x0[nl + 3 * m + 1:nl + 3 * m + 3] = (T + rot(th) @ c - c) / cw
    if start is not None:
        x0[:nl] = start.lens[:nl]

    n_all = max(comp) + 1

    def unpack(x):
        lens = np.zeros(N_LENS)
        lens[:nl] = x[:nl]
        pose = np.zeros((n_all, 3))
        pose[free] = x[nl:].reshape(-1, 3)
        return lens, pose

    def resid(x):
        lens, pose = unpack(x)
        xi, yi = Model.forward_arrays(lens, pose[I, 0], pose[I, 1], pose[I, 2], P[:, 0], P[:, 1])
        xj, yj = Model.forward_arrays(lens, pose[J, 0], pose[J, 1], pose[J, 2], Q[:, 0], Q[:, 1])
        return np.concatenate([xi - xj, yi - yj]) * cw

    npts = len(I)
    # Weak prior pulling aspect, shear and perspective towards zero. When every photo has the
    # same orientation (a straight scan) these can't be told apart from stretching the whole
    # mosaic, and without the prior the solver crawls along that flat valley. It is far too
    # weak to bias sections where the data does determine them.
    n_prior = min(nl, 4)
    prior_w = 0.02 * math.sqrt(npts)

    def resid_with_prior(x):
        return np.concatenate([resid(x), x[:n_prior] * cw * prior_w])

    sparsity = lil_matrix((2 * npts + n_prior, len(x0)), dtype=np.int8)
    sparsity[:2 * npts, :nl] = 1
    for k in range(n_prior):
        sparsity[2 * npts + k, k] = 1
    rows = np.arange(npts)
    for K in (I, J):
        ok = K != ref
        cols = nl + 3 * np.array([idx.get(k, 0) for k in K])
        for off in range(3):
            sparsity[rows[ok], cols[ok] + off] = 1
            sparsity[rows[ok] + npts, cols[ok] + off] = 1

    t_start, it = time.monotonic(), [0]
    npts_total = max(1, 2 * npts)

    def on_iteration(intermediate_result):
        it[0] += 1
        elapsed = time.monotonic() - t_start
        if rep is not None:
            rms = math.sqrt(2 * intermediate_result.cost / npts_total)
            rep.tick(f"iteration {it[0]}, residual {rms:.2f}px, {elapsed:.0f}s")
        if time_limit and elapsed > time_limit:
            raise StopIteration

    res = least_squares(resid_with_prior, x0, jac_sparsity=sparsity, loss="soft_l1", f_scale=1.0,
                        x_scale="jac", method="trf", ftol=1e-6, callback=on_iteration)
    if res.status == -2 and rep is not None:
        rep.log(f"    alignment solve stopped at the {time_limit:.0f}s time limit; using the best "
                "solution found so far", "warning")
    lens, pose = unpack(res.x)
    r = resid(res.x)
    r = np.hypot(r[:npts], r[npts:])
    edge_res = np.array([np.median(seg) for seg in np.split(r, np.cumsum(counts)[:-1])])
    model = Model(size, lens, {k: tuple(pose[k]) for k in comp})
    return model, edges, edge_res


def align(feats, size, opts, px_scale, names, rep):
    n = len(feats)
    ransac_px = opts.ransac_px * px_scale  # features are located at match scale
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]

    raw = pmap(rep, lambda ij: raw_matches(feats[ij[0]], feats[ij[1]], opts.min_inliers), pairs,
               "Matching photo pairs", opts.workers)
    fixed = CameraFixed(raw, size)
    n_fixed = [0]

    def job(k):
        p, q = raw[k]
        keep = fixed.keep(p, q)
        if (~keep).sum() >= opts.min_inliers:
            n_fixed[0] += 1
        m = fit_pair(p[keep], q[keep], opts.min_inliers, ransac_px)
        if m is None:
            return None
        i, j = pairs[k]
        return {"i": i, "j": j, "p": m[0], "q": m[1], "theta": m[2], "t": m[3]}

    edges = [e for e in pmap(rep, job, range(len(pairs)), "Checking matches", opts.workers) if e is not None]
    if n_fixed[0]:
        rep.log(f"    ignored features fixed to the camera (dust, scratches or glare that don't move with "
                f"the sample) in {n_fixed[0]} photo pairs")
    if not edges:
        raise RuntimeError("no overlapping image pairs were found")

    weight = Counter()
    for e in edges:
        weight[e["i"]] += len(e["p"])
        weight[e["j"]] += len(e["p"])
    ref = weight.most_common(1)[0][0]

    # Drop gross false matches first: chain the strongest overlaps into a rough layout and
    # discard overlaps that put a photo far from where that layout has it. Leaving them to the
    # robust solve works too, but can make it crawl for minutes.
    n_found = len(edges)
    init = initial_poses(edges, ref)
    gross = max(0.1 * size[0], 20 * px_scale)
    kept = []
    for e in edges:
        if e["i"] in init and e["j"] in init:
            (ti, Ti), (tj, Tj) = init[e["i"]], init[e["j"]]
            d = e["p"] @ rot(ti).T + Ti - (e["q"] @ rot(tj).T + Tj)
            err = float(np.median(np.hypot(d[:, 0], d[:, 1])))
            if err > gross:
                rep.log(f"    ignoring inconsistent overlap {names[e['i']]} / {names[e['j']]} "
                        f"(error {err:.0f}px)")
                continue
        kept.append(e)
    edges = kept

    # Solve, drop overlaps that disagree with the consensus (false matches), repeat.
    for n_pass in range(1, 7):
        rep.busy("Solving alignment" + (f" (pass {n_pass})" if n_pass > 1 else ""))
        model, edges, res = solve_global(edges, ref, size, opts.perspective, rep=rep,
                                         time_limit=opts.solve_time_limit)
        limit = max(2.0 * px_scale, 5 * float(np.median(res)))
        bad = np.flatnonzero(res > limit)
        if len(bad) == 0:
            break
        worst = res[bad].max()
        drop = {int(b) for b in bad if res[b] > worst / 2}
        for b in drop:
            rep.log(f"    ignoring inconsistent overlap {names[edges[b]['i']]} / {names[edges[b]['j']]} "
                    f"(error {res[b]:.1f}px)")
        edges = [e for k, e in enumerate(edges) if k not in drop]
    rejected = n_found - len(edges)
    if rejected > max(3, 0.25 * n_found):
        rep.log(f"    {rejected} of {n_found} photo overlaps disagreed with the rest and were ignored. This "
                "usually means repeated structures or features that don't move with the sample; check "
                "the layout image.", "warning")
    return model, edges, res, ref


def refine(model, edges, ref, paths, opts, rep, rounds=2):
    """Replace feature matches with dense full-resolution measurements and re-solve.

    For every overlap, both photos are rendered into the mosaic at full resolution over a grid
    of tiles; phase correlation measures any remaining offset to a fraction of a pixel. Those
    offsets become new correspondences for the global solve, so the final alignment is set
    by the actual image content at full resolution rather than by downscaled feature points.
    """
    rep.busy("Loading photos for refinement")
    gray = {}
    for k in model.poses:
        rep.check()
        im = cv2.imread(paths[k], cv2.IMREAD_GRAYSCALE).astype(np.float32)
        # remove slow illumination changes; keep the growth-line texture
        gray[k] = im - cv2.GaussianBlur(im, (0, 0), 8)
    tile = int(np.clip(model.w / 8, 96, 256))
    win = cv2.createHanningWindow((tile, tile), cv2.CV_32F)
    off = np.arange(tile, dtype=np.float64) - tile / 2

    def render(k, cx, cy):
        X, Y = np.meshgrid(cx + off, cy + off)
        mx, my = model.from_mosaic(k, X, Y)
        return cv2.remap(gray[k], mx, my, cv2.INTER_CUBIC)

    def inside(k, X, margin):
        mx, my = model.from_mosaic(k, X[:, 0], X[:, 1])
        return (mx > margin) & (my > margin) & (mx < model.w - margin) & (my < model.h - margin)

    def measure(e):
        i, j = e["i"], e["j"]
        oi, oj = model.outline(i), model.outline(j)
        lo = np.maximum(oi.min(0), oj.min(0))
        hi = np.minimum(oi.max(0), oj.max(0))
        if np.any(hi - lo < tile):
            return None
        step = max(tile * 0.75, float(np.sqrt(np.prod(hi - lo) / 80)))  # at most ~80 tiles
        gx, gy = np.meshgrid(np.arange(lo[0], hi[0], step), np.arange(lo[1], hi[1], step))
        C = np.c_[gx.ravel(), gy.ravel()]
        C = C[inside(i, C, tile * 0.75) & inside(j, C, tile * 0.75)]
        P, Q = [], []
        for cx, cy in C:
            a, b = render(i, cx, cy), render(j, cx, cy)
            if a.std() < 1.0 or b.std() < 1.0:
                continue
            (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
            if resp < 0.15 or abs(dx) > tile / 4 or abs(dy) > tile / 4:
                continue
            # content at mosaic point c in photo i sits at c + d in photo j's rendering
            pi = np.array(model.from_mosaic(i, np.array([cx]), np.array([cy]))).ravel()
            pj = np.array(model.from_mosaic(j, np.array([cx + dx]), np.array([cy + dy]))).ravel()
            P.append(pi)
            Q.append(pj)
        if len(P) < 4:
            return None
        return dict(e, p=np.float32(P), q=np.float32(Q))

    for r in range(rounds):
        dense = pmap(rep, measure, edges, f"Refining overlaps ({r + 1}/{rounds})", opts.workers)
        # keep feature matches for overlaps too small or plain for dense measurement
        new = [d if d is not None else e for d, e in zip(dense, edges)]
        rep.busy("Solving refined alignment")
        model, new, res = solve_global(new, ref, (model.w, model.h), opts.perspective, start=model,
                                       rep=rep, time_limit=opts.solve_time_limit)
        edges = new
    return model, edges, res


# ----------------------------------------------------------------------------- compositing

class Sampler:
    """Warps photos into rectangular regions of the mosaic, with feathering weights."""

    def __init__(self, model, paths, photo, x0, y0, scale, power):
        self.m, self.paths, self.photo = model, paths, photo
        self.x0, self.y0, self.scale, self.power = x0, y0, scale, power
        self.cache = {}
        # pre-shrink photos for small previews to avoid aliasing
        self.shrink = min(1.0, 2 * scale)
        self.boxes = {}
        for k in model.poses:
            o = (model.outline(k) - [x0, y0]) * scale
            self.boxes[k] = (o[:, 0].min(), o[:, 1].min(), o[:, 0].max(), o[:, 1].max())

    def image(self, k):
        if k not in self.cache:
            im = read_image(self.paths[k])
            if self.shrink < 1:
                im = cv2.resize(im, None, fx=self.shrink, fy=self.shrink, interpolation=cv2.INTER_AREA)
            self.cache[k] = im
        return self.cache[k]

    def forget_above(self, y):
        for k in [k for k in self.cache if self.boxes[k][3] < y]:
            del self.cache[k]

    def sample(self, k, ry0, ry1, rx0, rx1):
        """Returns (pixels float32 HxWx3, weight float32 HxW) for output rows/cols given."""
        m = self.m
        ys = (np.arange(ry0, ry1, dtype=np.float64) / self.scale) + self.y0
        xs = (np.arange(rx0, rx1, dtype=np.float64) / self.scale) + self.x0
        X, Y = np.meshgrid(xs, ys)
        mx, my = m.from_mosaic(k, X, Y)
        # feather weight from distance to the photo's own border
        dx = np.minimum(mx + 0.5, m.w - 0.5 - mx) / (m.w / 2)
        dy = np.minimum(my + 0.5, m.h - 0.5 - my) / (m.h / 2)
        wgt = np.clip(np.minimum(dx, dy), 0, None) ** self.power
        f = self.shrink
        im = self.image(k)
        px = cv2.remap(im, (mx + 0.5) * f - 0.5, (my + 0.5) * f - 0.5, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        px = px.astype(np.float32) * self.photo.factor(k, mx, my)
        return px, wgt.astype(np.float32)


def composite(sampler, out, rep, desc, strip=1024, rgb=False):
    """Blend all photos into `out` (H x W x 3 uint8, may be a memmap), strip by strip.

    Pixels are BGR (OpenCV order) unless rgb=True.
    """
    H, W = out.shape[:2]
    order = sorted(sampler.boxes, key=lambda k: sampler.boxes[k][1])
    rows = range(0, H, strip)
    for sy in rep.track(rows, len(rows), desc):
        sh = min(strip, H - sy)
        acc = np.zeros((sh, W, 3), np.float32)
        wsum = np.zeros((sh, W), np.float32)
        for k in order:
            bx0, by0, bx1, by1 = sampler.boxes[k]
            if by1 < sy - 1 or by0 > sy + sh + 1:
                continue
            cx0, cx1 = max(0, int(bx0) - 1), min(W, int(math.ceil(bx1)) + 2)
            ry0, ry1 = max(sy, int(by0) - 1), min(sy + sh, int(math.ceil(by1)) + 2)
            px, wgt = sampler.sample(k, ry0, ry1, cx0, cx1)
            acc[ry0 - sy:ry1 - sy, cx0:cx1] += px * wgt[..., None]
            wsum[ry0 - sy:ry1 - sy, cx0:cx1] += wgt
        sampler.forget_above(sy + sh)
        res = np.zeros((sh, W, 3), np.uint8)
        valid = wsum > 1e-8
        res[valid] = np.clip(acc[valid] / wsum[valid][:, None] + 0.5, 0, 255).astype(np.uint8)
        out[sy:sy + sh] = res[..., ::-1] if rgb else res


def _flat_basis(xn, yn):
    """Smooth 2D polynomial terms (no constant) describing uneven illumination."""
    return np.stack([xn, yn, xn * xn, xn * yn, yn * yn,
                     xn ** 3, xn * xn * yn, xn * yn * yn, yn ** 3], -1)


class Photometric:
    """Per-photo exposure gains plus a shared illumination (flat-field) pattern.

    corrected = pixel * gain[k] / flat(x, y), where flat is the same for every photo.
    """

    def __init__(self, model, log_gains=None, flat=None):
        self.m = model
        self.log_gains = log_gains or {k: np.zeros(3) for k in model.poses}
        self.flat = np.zeros(9) if flat is None else flat

    def factor(self, k, mx, my):
        xn = (mx - self.m.c[0]) / self.m.cw
        yn = (my - self.m.c[1]) / self.m.cw
        log_flat = _flat_basis(xn, yn) @ self.flat
        return np.exp(self.log_gains[k][None, None, :] - log_flat[..., None]).astype(np.float32)

    def gains(self, k):
        return np.exp(self.log_gains[k])

    @classmethod
    def estimate(cls, model, edges, paths, fit_flat=True, n_samples=600):
        """Least squares on log intensities of the same sample points seen in two photos."""
        m = model
        small_w = 256
        f = small_w / m.w
        imgs = {}
        for k in m.poses:
            im = cv2.resize(read_image(paths[k]), None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            imgs[k] = cv2.GaussianBlur(im.astype(np.float32), (0, 0), 1.5)
        keys = sorted(m.poses)
        col = {k: n for n, k in enumerate(keys)}
        nf = 9 if fit_flat else 0

        def sample(k, X):
            mx, my = m.from_mosaic(k, X[:, 0], X[:, 1])
            v = cv2.remap(imgs[k], ((mx + 0.5) * f - 0.5)[None], ((my + 0.5) * f - 0.5)[None],
                          cv2.INTER_LINEAR)[0]
            return mx, my, v

        rows_B, rows_i, rows_j, rhs = [], [], [], []
        rng = np.random.default_rng(1)
        margin = 0.02 * m.w
        for e in edges:
            i, j = e["i"], e["j"]
            oi, oj = m.outline(i), m.outline(j)
            lo, hi = np.maximum(oi.min(0), oj.min(0)), np.minimum(oi.max(0), oj.max(0))
            if np.any(hi <= lo):
                continue
            X = lo + rng.random((n_samples * 3, 2)) * (hi - lo)
            xi, yi, vi = sample(i, X)
            xj, yj, vj = sample(j, X)
            ok = ((xi > margin) & (yi > margin) & (xi < m.w - margin) & (yi < m.h - margin) &
                  (xj > margin) & (yj > margin) & (xj < m.w - margin) & (yj < m.h - margin) &
                  (vi.min(1) > 8) & (vj.min(1) > 8) & (vi.max(1) < 245) & (vj.max(1) < 245))
            ok = np.flatnonzero(ok)[:n_samples]
            if len(ok) < 20:
                continue
            Bi = _flat_basis((xi[ok] - m.c[0]) / m.cw, (yi[ok] - m.c[1]) / m.cw)
            Bj = _flat_basis((xj[ok] - m.c[0]) / m.cw, (yj[ok] - m.c[1]) / m.cw)
            for ch in range(3):
                rows_B.append(Bi - Bj)
                rows_i.append(np.full(len(ok), 3 * col[i] + ch))
                rows_j.append(np.full(len(ok), 3 * col[j] + ch))
                rhs.append(np.log(vj[ok, ch]) - np.log(vi[ok, ch]))
        if not rhs:
            return cls(model)
        B = np.concatenate(rows_B)
        ci, cj, y = np.concatenate(rows_i), np.concatenate(rows_j), np.concatenate(rhs)
        n_eq, n_g = len(y), 3 * len(keys)
        # unknowns: [flat (nf)] + [log gain per photo & channel]
        # equation: log v_i - B_i.c + g_i = log v_j - B_j.c + g_j
        from scipy.sparse import coo_matrix, vstack, identity
        from scipy.sparse.linalg import lsqr
        r = np.arange(n_eq)
        parts = [coo_matrix((np.ones(n_eq), (r, ci)), (n_eq, n_g)),
                 coo_matrix((-np.ones(n_eq), (r, cj)), (n_eq, n_g))]
        G = parts[0] + parts[1]
        if nf:
            from scipy.sparse import hstack
            M = hstack([coo_matrix(-B), G]).tocsr()
        else:
            M = G.tocsr()
        # weak priors keep gains near 1 and the flat field near uniform
        lam = np.sqrt(n_eq / (n_g + nf)) * 0.05
        prior = identity(nf + n_g, format="csr") * lam
        w = np.ones(n_eq)
        for _ in range(5):  # iteratively reweighted: highlights/reflections differ between shots
            sol = lsqr(vstack([M.multiply(w[:, None]), prior]).tocsr(),
                       np.r_[y * w, np.zeros(nf + n_g)], atol=1e-10, btol=1e-10)[0]
            res = M @ sol - y
            scale = 1.4826 * np.median(np.abs(res)) + 1e-6
            w = 1 / np.sqrt(np.maximum(1, np.abs(res) / (2 * scale)))
        flat = sol[:nf] if nf else np.zeros(9)
        g = sol[nf:].reshape(-1, 3)
        g -= np.median(g, 0)  # overall brightness follows the typical photo
        return cls(model, {k: g[col[k]] for k in keys}, flat)


# ----------------------------------------------------------------------------- driver

def _lens_max_shift_px(model):
    """Largest displacement the camera/lens correction applies anywhere on a photo's border."""
    t = np.linspace(0, 1, 16, endpoint=False)
    w, h = model.w - 1, model.h - 1
    raw = np.concatenate([np.c_[t * w, 0 * t], np.c_[w + 0 * t, t * h],
                          np.c_[w - t * w, h + 0 * t], np.c_[0 * t, h - t * h]]) - model.c
    u, v = Model.forward_arrays(model.lens, 0.0, 0.0, 0.0, raw[:, 0] / model.cw, raw[:, 1] / model.cw)
    return float(np.hypot(u * model.cw - raw[:, 0], v * model.cw - raw[:, 1]).max())


def _illumination_range_pct(photo, model):
    if not np.any(photo.flat):
        return 0.0
    ys, xs = np.mgrid[0:model.h:model.h / 24, 0:model.w:model.w / 32]
    xn, yn = (xs - model.c[0]) / model.cw, (ys - model.c[1]) / model.cw
    v = _flat_basis(xn, yn) @ photo.flat
    return float((math.exp(v.max() - v.min()) - 1) * 100)


def split_by_camera_setting(paths, rep):
    """Keep the photos that share the folder's main image size and magnification.

    Photos taken at a different zoom or saved at a different size can't be combined in a
    measurement-grade mosaic, so they are left out with the reason recorded. Returns
    (kept paths, [{file, reason}], micrometres per pixel of the kept photos or None).
    """
    sizes = {p: image_size(p) for p in paths}
    size = Counter(sizes.values()).most_common(1)[0][0]
    excluded = [{"file": os.path.basename(p), "reason": f"different image size ({s[0]}×{s[1]} px, "
                 f"the rest are {size[0]}×{size[1]} px)"} for p, s in sizes.items() if s != size]
    paths = [p for p in paths if sizes[p] == size]
    cal = {p: leica_um_per_px(p, size[0]) for p in paths}
    known = Counter(round(c, 3) for c in cal.values() if c)
    um_per_px = known.most_common(1)[0][0] if known else None
    if len(known) > 1:
        odd = [p for p in paths if cal[p] and round(cal[p], 3) != um_per_px]
        for p in odd:
            excluded.append({"file": os.path.basename(p),
                             "reason": f"different magnification ({cal[p]:.2f} µm/px, the rest are "
                                       f"{um_per_px:.2f} µm/px)"})
        paths = [p for p in paths if p not in odd]
    for reason in sorted({e["reason"] for e in excluded}):
        files = [e["file"] for e in excluded if e["reason"] == reason]
        rep.log(f"    left out {', '.join(files)}: {reason}", "warning")
    return paths, excluded, um_per_px


def stitch_folder(folder, opts=None, rep=None):
    """Stitch one folder of photos. Returns a result dict (status: done/preview/skipped)."""
    opts = opts or Options()
    rep = rep or Reporter()
    name = os.path.basename(os.path.normpath(folder))
    out = output_paths(opts.output, name)
    rep.section(name)
    if opts.skip_existing:
        done = out["preview"] if opts.preview_only else out["tif"]
        if os.path.exists(done):
            rep.log(f"[{name}] already stitched ({done}), skipping")
            return {"name": name, "status": "skipped", "message": "already stitched", "outputs": out}
    paths = list_images(folder, opts.exclude)
    if len(paths) < 2:
        rep.log(f"[{name}] fewer than two images, skipping", "warning")
        return {"name": name, "status": "skipped", "message": "fewer than two images", "outputs": out}
    t0 = time.time()
    rep.log(f"[{name}] {len(paths)} images")
    paths, excluded, um_per_px = split_by_camera_setting(paths, rep)
    if len(paths) < 2:
        rep.log(f"[{name}] fewer than two compatible images, skipping", "warning")
        return {"name": name, "status": "skipped", "message": "fewer than two compatible images",
                "outputs": out}

    feats = pmap(rep, lambda p: extract_features(p, opts.match_width, opts.features), paths,
                 "Finding features", opts.workers)
    size = feats[0][2]  # all the same: see split_by_camera_setting
    names = [os.path.basename(p) for p in paths]

    px_scale = max(1.0, size[0] / opts.match_width)
    model, edges, res, ref = align(feats, size, opts, px_scale, names, rep)
    del feats
    rep.log(f"    initial alignment: typical error {np.median(res):.2f}px ({time.time() - t0:.0f}s)")
    if opts.refine:
        model, edges, res = refine(model, edges, ref, paths, opts, rep)
    placed = sorted(model.poses)
    missing = [names[k] for k in range(len(paths)) if k not in model.poses]
    rep.log(f"    aligned {len(placed)}/{len(paths)} images using {len(edges)} overlaps, "
            f"typical error {np.median(res):.2f}px, worst overlap {res.max():.2f}px ({time.time() - t0:.0f}s)")
    if missing:
        rep.log(f"    WARNING: could not place {', '.join(missing)} (no reliable overlap)", "warning")

    outline = np.vstack([model.outline(k) for k in placed])
    x0, y0 = np.floor(outline.min(0))
    x1, y1 = np.ceil(outline.max(0))
    W, H = int(x1 - x0) + 1, int(y1 - y0) + 1

    if opts.gain:
        rep.busy("Correcting exposure and illumination")
        photo = Photometric.estimate(model, edges, paths, opts.flat)
    else:
        photo = Photometric(model)
    power = 40.0 if opts.blend == "seam" else opts.feather_power

    os.makedirs(opts.output, exist_ok=True)

    # preview + layout diagram first, so problems are visible before the long full-res pass
    ps = min(1.0, opts.preview_width / W)
    prev = np.zeros((int(H * ps) + 1, int(W * ps) + 1, 3), np.uint8)
    composite(Sampler(model, paths, photo, x0, y0, ps, power), prev, rep, "Rendering preview", strip=512)
    cv2.imwrite(out["preview"], prev, [cv2.IMWRITE_JPEG_QUALITY, 90])
    layout = prev.copy()
    fs = max(0.35, prev.shape[1] / 5000)
    for k in placed:
        o = (model.outline(k) - [x0, y0]) * ps
        cv2.polylines(layout, [np.int32(o)], True, (0, 255, 255), 1, cv2.LINE_AA)
        label = os.path.splitext(names[k])[0]
        label = label.replace("image", "") or label
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        ctr = model.to_mosaic(k, [model.c])[0]
        org = np.int32((ctr - [x0, y0]) * ps - [tw / 2, -th / 2])
        cv2.putText(layout, label, tuple(org), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(layout, label, tuple(org), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.imwrite(out["layout"], layout, [cv2.IMWRITE_JPEG_QUALITY, 90])

    report = {
        "section": name,
        "folder": os.path.abspath(folder),
        "created": datetime.now().isoformat(timespec="seconds"),
        "software": f"shellstitch {__version__}",
        "width_px": W, "height_px": H,
        "preview_width_px": prev.shape[1],
        "um_per_px": um_per_px,
        "photo_size_px": [model.w, model.h],
        "n_photos": len(paths),
        "camera": dict(zip(["aspect", "shear", "persp_x", "persp_y", "radial_k1"],
                           map(float, model.lens))),
        "lens_max_shift_px": round(_lens_max_shift_px(model), 2),
        "illumination_range_pct": round(_illumination_range_pct(photo, model), 1),
        "median_error_px": float(np.median(res)),
        "images": [{"file": names[k],
                    "centre_xy": (model.to_mosaic(k, [model.c])[0] - [x0, y0]).round(1).tolist(),
                    "rotation_deg": round(model.rotation_deg(k), 3),
                    "gain_bgr": [round(float(g), 4) for g in photo.gains(k)]} for k in placed],
        "unplaced": missing,
        "excluded": excluded,
        "overlaps": [{"a": names[e["i"]], "b": names[e["j"]], "matches": len(e["p"]),
                      "error_px": round(float(r), 3)} for e, r in zip(edges, res)],
        "options": opts.to_dict(),
        "full_resolution_written": not opts.preview_only,
    }

    def write_report():
        report["elapsed_s"] = round(time.time() - t0, 1)
        with open(out["report"], "w") as fh:
            json.dump(report, fh, indent=1)

    result = {"name": name, "outputs": out, "median_error_px": report["median_error_px"],
              "placed": len(placed), "total": len(paths), "unplaced": missing, "excluded": excluded}
    if opts.preview_only:
        write_report()
        rep.log(f"[{name}] preview written ({time.time() - t0:.0f}s)")
        return dict(result, status="preview")

    rep.log(f"    writing {W} x {H} px mosaic ({W * H * 3 / 1e9:.2f} GB)")
    kw = {}
    if um_per_px:
        ppcm = 1e4 / um_per_px
        kw = {"resolution": (ppcm, ppcm), "resolutionunit": "CENTIMETER"}
    desc = f"Stitched from {len(placed)} images of {name}"
    if um_per_px:
        desc += f"; {um_per_px:.4f} um/px"
    tmp = out["tif"] + ".partial"
    try:
        mm = tifffile.memmap(tmp, shape=(H, W, 3), dtype=np.uint8, photometric="rgb",
                             bigtiff=W * H * 3 > 3.9e9, description=desc, **kw)
        composite(Sampler(model, paths, photo, x0, y0, 1.0, power), mm, rep, "Writing mosaic", rgb=True)
        mm.flush()
        del mm
        os.replace(tmp, out["tif"])
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    write_report()
    rep.log(f"[{name}] done in {time.time() - t0:.0f}s -> {out['tif']}")
    return dict(result, status="done")
