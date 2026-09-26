"""Stitch overlapping microscope photos of a shell section into one full-resolution mosaic.

Each input folder holds overlapping close-ups of one section, captured in any order or
scan pattern. Every image is matched against every other image with SIFT features, then all
matches are combined in one global least-squares adjustment. The model is physical: the
sample moves rigidly under a fixed camera (rotation + translation per photo, no scaling),
and the camera's slight oblique view and lens distortion are shared by every photo and
solved for at the same time. Exposure differences are evened out and the photos are
blended onto one canvas, written as an uncompressed TIFF plus a JPEG preview.

Usage:
    uv run stitch.py images/M27                 # one folder
    uv run stitch.py images/*/                  # several folders
    uv run stitch.py images/M27 -o out --blend seam
"""

import argparse
import fnmatch
import json
import math
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import tifffile
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from tqdm import tqdm

IMAGE_EXTS = (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp")
N_LENS = 5  # shared camera parameters: aspect, shear, 2x perspective, radial distortion


def log(*a):
    print(*a, flush=True)


def bar(iterable, total, desc):
    """Progress bar for one stage of a folder's stitch."""
    return tqdm(iterable, total=total, desc=f"    {desc}", unit="", leave=True, ncols=90,
                bar_format="{desc:<24}{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")


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


def match_pair(fa, fb, min_inliers, ransac_px, ratio=0.85):
    """Matches between two images, or None. Returns inlier points and a rough rigid fit."""
    pa, da, _ = fa
    pb, db, _ = fb
    if len(da) < 8 or len(db) < 8:
        return None
    matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
    knn = matcher.knnMatch(da, db, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < ratio * m[1].distance]
    if len(good) < min_inliers:
        return None
    p = pa[[g.queryIdx for g in good]]
    q = pb[[g.trainIdx for g in good]]
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


def solve_global(edges, ref, size, fit_lens, max_pts=200, start=None):
    """Robust least squares over all matches; returns the Model and per-edge median residual."""
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
    sparsity = lil_matrix((2 * npts, len(x0)), dtype=np.int8)
    sparsity[:, :nl] = 1
    rows = np.arange(npts)
    for K in (I, J):
        ok = K != ref
        cols = nl + 3 * np.array([idx.get(k, 0) for k in K])
        for off in range(3):
            sparsity[rows[ok], cols[ok] + off] = 1
            sparsity[rows[ok] + npts, cols[ok] + off] = 1

    res = least_squares(resid, x0, jac_sparsity=sparsity, loss="soft_l1", f_scale=1.0,
                        x_scale="jac", method="trf")
    lens, pose = unpack(res.x)
    r = resid(res.x)
    r = np.hypot(r[:npts], r[npts:])
    edge_res = np.array([np.median(seg) for seg in np.split(r, np.cumsum(counts)[:-1])])
    model = Model(size, lens, {k: tuple(pose[k]) for k in comp})
    return model, edges, edge_res


def align(feats, size, args, px_scale, names):
    n = len(feats)
    ransac_px = args.ransac_px * px_scale  # features are located at match scale
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]

    def job(ij):
        m = match_pair(feats[ij[0]], feats[ij[1]], args.min_inliers, ransac_px)
        if m is None:
            return None
        p, q, theta, t = m
        return {"i": ij[0], "j": ij[1], "p": p, "q": q, "theta": theta, "t": t}

    with ThreadPoolExecutor(args.workers) as ex:
        edges = [e for e in bar(ex.map(job, pairs), len(pairs), "matching pairs") if e is not None]
    if not edges:
        raise RuntimeError("no overlapping image pairs were found")

    weight = Counter()
    for e in edges:
        weight[e["i"]] += len(e["p"])
        weight[e["j"]] += len(e["p"])
    ref = weight.most_common(1)[0][0]

    # Solve, drop overlaps that disagree with the consensus (false matches), repeat.
    for _ in range(6):
        model, edges, res = solve_global(edges, ref, size, args.perspective)
        limit = max(2.0 * px_scale, 5 * float(np.median(res)))
        bad = np.flatnonzero(res > limit)
        if len(bad) == 0:
            break
        worst = res[bad].max()
        drop = {int(b) for b in bad if res[b] > worst / 2}
        for b in drop:
            log(f"    ignoring inconsistent overlap {names[edges[b]['i']]} / {names[edges[b]['j']]} "
                f"(error {res[b]:.1f}px)")
        edges = [e for k, e in enumerate(edges) if k not in drop]
    return model, edges, res, ref


def refine(model, edges, ref, paths, args, rounds=2):
    """Replace feature matches with dense full-resolution measurements and re-solve.

    For every overlap, both photos are rendered into the mosaic at full resolution over a grid
    of tiles; phase correlation measures any remaining offset to a fraction of a pixel. Those
    offsets become new correspondences for the global solve, so the final alignment is set
    by the actual image content at full resolution rather than by downscaled feature points.
    """
    gray = {}
    for k in model.poses:
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
        with ThreadPoolExecutor(args.workers) as ex:
            dense = list(bar(ex.map(measure, edges), len(edges), f"refining ({r + 1}/{rounds})"))
        # keep feature matches for overlaps too small or plain for dense measurement
        new = [d if d is not None else e for d, e in zip(dense, edges)]
        model, new, res = solve_global(new, ref, (model.w, model.h), args.perspective, start=model)
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


def composite(sampler, out, strip=1024, progress=False, rgb=False):
    """Blend all photos into `out` (H x W x 3 uint8, may be a memmap), strip by strip.

    Pixels are BGR (OpenCV order) unless rgb=True.
    """
    H, W = out.shape[:2]
    order = sorted(sampler.boxes, key=lambda k: sampler.boxes[k][1])
    rows = range(0, H, strip)
    for sy in (bar(rows, len(rows), "writing mosaic") if progress else rows):
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

def stitch_folder(folder, args):
    name = os.path.basename(os.path.normpath(folder))
    base = os.path.join(args.output, name)
    if args.skip_existing:
        done = base + ("_preview.jpg" if args.preview_only else ".tif")
        if os.path.exists(done):
            log(f"[{name}] already stitched ({done}), skipping")
            return
    paths = list_images(folder, args.exclude)
    if len(paths) < 2:
        log(f"[{name}] fewer than two images, skipping")
        return
    t0 = time.time()
    log(f"[{name}] {len(paths)} images")

    with ThreadPoolExecutor(args.workers) as ex:
        feats = list(bar(ex.map(lambda p: extract_features(p, args.match_width, args.features), paths),
                         len(paths), "finding features"))
    size = Counter(f[2] for f in feats).most_common(1)[0][0]
    odd = [os.path.basename(p) for p, f in zip(paths, feats) if f[2] != size]
    if odd:
        log(f"    skipping {', '.join(odd)}: different image size from the rest")
        keep = [f[2] == size for f in feats]
        paths = [p for p, k in zip(paths, keep) if k]
        feats = [f for f, k in zip(feats, keep) if k]

    px_scale = max(1.0, size[0] / args.match_width)
    model, edges, res, ref = align(feats, size, args, px_scale, [os.path.basename(p) for p in paths])
    log(f"    initial alignment: typical error {np.median(res):.2f}px ({time.time() - t0:.0f}s)")
    if args.refine:
        model, edges, res = refine(model, edges, ref, paths, args)
    placed = sorted(model.poses)
    missing = [os.path.basename(paths[k]) for k in range(len(paths)) if k not in model.poses]
    log(f"    aligned {len(placed)}/{len(paths)} images using {len(edges)} overlaps, "
        f"typical error {np.median(res):.2f}px, worst overlap {res.max():.2f}px ({time.time() - t0:.0f}s)")
    if missing:
        log(f"    WARNING: could not place {', '.join(missing)} (no reliable overlap)")

    outline = np.vstack([model.outline(k) for k in placed])
    x0, y0 = np.floor(outline.min(0))
    x1, y1 = np.ceil(outline.max(0))
    W, H = int(x1 - x0) + 1, int(y1 - y0) + 1

    photo = (Photometric.estimate(model, edges, paths, args.flat) if args.gain
             else Photometric(model))
    power = 40.0 if args.blend == "seam" else args.feather_power

    os.makedirs(args.output, exist_ok=True)

    # preview + layout diagram first, so problems are visible before the long full-res pass
    ps = min(1.0, args.preview_width / W)
    prev = np.zeros((int(H * ps) + 1, int(W * ps) + 1, 3), np.uint8)
    composite(Sampler(model, paths, photo, x0, y0, ps, power), prev, strip=4096)
    cv2.imwrite(base + "_preview.jpg", prev, [cv2.IMWRITE_JPEG_QUALITY, 90])
    layout = prev.copy()
    fs = max(0.35, prev.shape[1] / 5000)
    for k in placed:
        o = (model.outline(k) - [x0, y0]) * ps
        cv2.polylines(layout, [np.int32(o)], True, (0, 255, 255), 1, cv2.LINE_AA)
        label = os.path.splitext(os.path.basename(paths[k]))[0]
        label = label.replace("image", "") or label
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        ctr = model.to_mosaic(k, [model.c])[0]
        org = np.int32((ctr - [x0, y0]) * ps - [tw / 2, -th / 2])
        cv2.putText(layout, label, tuple(org), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(layout, label, tuple(org), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.imwrite(base + "_layout.jpg", layout, [cv2.IMWRITE_JPEG_QUALITY, 90])

    # calibration from Leica metadata (most common value in the folder)
    cal = [round(c, 3) for c in (leica_um_per_px(p, model.w) for p in paths) if c]
    um_per_px = Counter(cal).most_common(1)[0][0] if cal else None
    if len(set(cal)) > 1:
        log(f"    note: calibration differs between photos {sorted(set(cal))} um/px; using {um_per_px}")

    names = [os.path.basename(p) for p in paths]
    report = {
        "folder": os.path.abspath(folder),
        "width_px": W, "height_px": H,
        "um_per_px": um_per_px,
        "camera": dict(zip(["aspect", "shear", "persp_x", "persp_y", "radial_k1"],
                           map(float, model.lens))),
        "median_error_px": float(np.median(res)),
        "images": [{"file": names[k],
                    "centre_xy": (model.to_mosaic(k, [model.c])[0] - [x0, y0]).round(1).tolist(),
                    "rotation_deg": round(model.rotation_deg(k), 3),
                    "gain_bgr": [round(float(g), 4) for g in photo.gains(k)]} for k in placed],
        "unplaced": missing,
        "overlaps": [{"a": names[e["i"]], "b": names[e["j"]], "matches": len(e["p"]),
                      "error_px": round(float(r), 3)} for e, r in zip(edges, res)],
    }
    with open(base + "_report.json", "w") as fh:
        json.dump(report, fh, indent=1)

    if args.preview_only:
        log(f"[{name}] preview written ({time.time() - t0:.0f}s)")
        return

    log(f"    writing {W} x {H} px mosaic ({W * H * 3 / 1e9:.2f} GB)")
    kw = {}
    if um_per_px:
        ppcm = 1e4 / um_per_px
        kw = {"resolution": (ppcm, ppcm), "resolutionunit": "CENTIMETER"}
    desc = f"Stitched from {len(placed)} images of {name}"
    if um_per_px:
        desc += f"; {um_per_px:.4f} um/px"
    tmp = base + ".tif.partial"
    out = tifffile.memmap(tmp, shape=(H, W, 3), dtype=np.uint8, photometric="rgb",
                          bigtiff=W * H * 3 > 3.9e9, description=desc, **kw)
    composite(Sampler(model, paths, photo, x0, y0, 1.0, power), out, progress=True, rgb=True)
    out.flush()
    del out
    os.replace(tmp, base + ".tif")
    log(f"[{name}] done in {time.time() - t0:.0f}s -> {base}.tif")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="folders of overlapping images (one mosaic per folder)")
    ap.add_argument("-o", "--output", default="stitched", help="output folder (default: stitched)")
    ap.add_argument("--exclude", action="append", default=["* - Copy.*"],
                    help="filename pattern to skip (repeatable; default skips '* - Copy.*')")
    ap.add_argument("--blend", choices=["feather", "seam"], default="feather",
                    help="feather: smooth transitions (default); seam: near-hard seams, no double edges")
    ap.add_argument("--feather-power", type=float, default=3.0,
                    help="higher = narrower transition between overlapping photos (default 3)")
    ap.add_argument("--no-gain", dest="gain", action="store_false",
                    help="disable exposure and illumination correction")
    ap.add_argument("--no-flat", dest="flat", action="store_false",
                    help="don't correct uneven illumination within each photo (gains only)")
    ap.add_argument("--no-perspective", dest="perspective", action="store_false",
                    help="don't model camera tilt / lens distortion (rigid alignment only)")
    ap.add_argument("--no-refine", dest="refine", action="store_false",
                    help="skip the full-resolution refinement pass")
    ap.add_argument("--match-width", type=int, default=1600,
                    help="photos are downscaled to this width for feature matching (default 1600)")
    ap.add_argument("--features", type=int, default=12000, help="max SIFT features per photo")
    ap.add_argument("--min-inliers", type=int, default=8, help="matches needed to accept an overlap")
    ap.add_argument("--ransac-px", type=float, default=2.0, help="match tolerance in px (at match scale)")
    ap.add_argument("--preview-width", type=int, default=4000)
    ap.add_argument("--preview-only", action="store_true", help="skip the full-resolution TIFF")
    ap.add_argument("-n", "--skip-existing", action="store_true",
                    help="only stitch folders that don't already have an output mosaic")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    failed = []
    for f in args.folders:
        try:
            stitch_folder(f, args)
        except Exception as e:  # keep going with the other folders
            log(f"[{f}] FAILED: {e}")
            failed.append(f)
            if len(args.folders) == 1:
                raise
    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
