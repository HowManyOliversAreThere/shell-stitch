"""End-to-end tests of every stitching setting against synthetic sections with a known truth.

Each test checks the *effect* of a setting, not just that it runs: where the photos were
placed (pos_err, px), whether the mosaic covers what the photos covered (coverage, 1.0 is
ideal) and how closely it matches the true scene (mae, grey levels). See helpers.evaluate.
"""

import os
from dataclasses import fields

import cv2
import numpy as np
import pytest
import tifffile

from helpers import COVERED, NONDEFAULT, covers, evaluate, stitch
from shellstitch.engine import find_scale_bar
from shellstitch.options import Options
from shellstitch.synthetic import make_section, write_calibration

# Limits for a correct stitch of the synthetic sections (typical values are ~3-10x lower).
POS_OK = 0.5      # px
MAE_OK = 1.6      # grey levels
COVERAGE_OK = (0.99, 1.01)


@pytest.fixture(scope="session")
def sections(tmp_path_factory):
    """Synthetic sections, each with one kind of imperfection to correct (made once)."""
    root = tmp_path_factory.mktemp("sections")
    kinds = {
        "plain": {},
        "lens": {"lens": True},
        "gains": {"gains": True},
        "vignette": {"vignette": True},
        "gradient": {"gradient": True},
        "all": {"lens": True, "gains": True, "vignette": True, "dust": True, "copy": True, "odd_zoom": True},
    }
    return {name: make_section(str(root / name), **kw) for name, kw in kinds.items()}


def assert_good(scores, pos=POS_OK, mae=MAE_OK):
    assert scores["pos_err"] < pos, scores
    lo, hi = COVERAGE_OK
    if "coverage" in scores:
        assert lo < scores["coverage"] < hi, scores
        assert scores["mae"] < mae, scores


# ----------------------------------------------------------------------------- defaults

def test_defaults_stitch_everything_accurately(sections, tmp_path):
    """All imperfections at once, default settings: accurate, calibrated, right photos used."""
    truth = sections["all"]
    result, report, mosaic, rec = stitch(truth, tmp_path)
    assert result["status"] == "done"
    assert result["placed"] == len(truth.photos) and not result["unplaced"]
    assert_good(evaluate(truth, report, mosaic))
    assert report["um_per_px"] == pytest.approx(truth.um_per_px, rel=1e-3)
    placed = {i["file"] for i in report["images"]}
    assert not placed & set(truth.skipped_by_default), "the '- Copy' duplicate should be skipped"
    assert [e["file"] for e in report["excluded"]] == truth.excluded
    assert "magnification" in report["excluded"][0]["reason"]


def test_camera_fixed_dust_does_not_disturb_alignment(tmp_path):
    truth = make_section(str(tmp_path / "dusty"), dust=True, seed=4)
    _, report, mosaic, _ = stitch(truth, tmp_path / "out")
    assert_good(evaluate(truth, report, mosaic))


# ----------------------------------------------------------------------------- alignment

@covers("perspective")
def test_perspective_corrects_camera_tilt_and_lens(sections, tmp_path):
    truth = sections["lens"]
    _, on, mosaic_on, _ = stitch(truth, tmp_path / "on")
    _, off, mosaic_off, _ = stitch(truth, tmp_path / "off", perspective=False)
    assert_good(evaluate(truth, on, mosaic_on))
    assert evaluate(truth, off, mosaic_off)["pos_err"] > 1.5, "without it, the lens distortion should show"
    assert all(v == 0 for v in off["camera"].values())
    assert on["camera"]["radial_k1"] == pytest.approx(truth.lens[4], abs=0.01)


@covers("refine")
def test_refine(sections, tmp_path):
    truth = sections["all"]
    _, on, mosaic_on, rec_on = stitch(truth, tmp_path / "on")
    _, off, mosaic_off, rec_off = stitch(truth, tmp_path / "off", refine=False)
    assert any(s.startswith("Refining overlaps") for s in rec_on.stages)
    assert not any(s.startswith("Refining overlaps") for s in rec_off.stages)
    assert_good(evaluate(truth, on, mosaic_on))
    assert_good(evaluate(truth, off, mosaic_off))


@covers("match_width")
def test_match_width(sections, tmp_path):
    truth = sections["all"]
    for width in (250, 4000):  # heavily downscaled, and larger than the photos
        _, report, mosaic, _ = stitch(truth, tmp_path / str(width), match_width=width)
        assert_good(evaluate(truth, report, mosaic))


@covers("features")
def test_features_limit(sections, tmp_path):
    truth = sections["plain"]
    _, few, mosaic, _ = stitch(truth, tmp_path / "few", features=600, refine=False)
    _, many, _, _ = stitch(truth, tmp_path / "many", features=12000, refine=False)
    assert_good(evaluate(truth, few, mosaic))
    assert sum(o["matches"] for o in few["overlaps"]) < sum(o["matches"] for o in many["overlaps"])


@covers("min_inliers")
def test_min_inliers(sections, tmp_path):
    truth = sections["plain"]
    need = 60
    _, report, _, _ = stitch(truth, tmp_path / "some", min_inliers=need, refine=False)
    assert all(o["matches"] >= need for o in report["overlaps"])
    with pytest.raises(RuntimeError, match="no overlapping"):
        stitch(truth, tmp_path / "none", min_inliers=100000)


@covers("ransac_px")
def test_ransac_tolerance(sections, tmp_path):
    truth = sections["plain"]
    _, strict, mosaic, _ = stitch(truth, tmp_path / "strict", ransac_px=0.5, refine=False)
    _, loose, _, _ = stitch(truth, tmp_path / "loose", ransac_px=8.0, refine=False)
    assert_good(evaluate(truth, strict, mosaic))
    assert sum(o["matches"] for o in strict["overlaps"]) < sum(o["matches"] for o in loose["overlaps"])


@covers("solve_time_limit")
def test_solve_time_limit_stops_early_but_still_stitches(sections, tmp_path):
    """A limit that's hit stops the solver after its first iteration: the stitch still
    completes with every photo placed, just less precisely (how much depends on the machine
    and on randomness in matching, so only a rough layout is checked)."""
    truth = sections["all"]
    result, report, mosaic, rec = stitch(truth, tmp_path, solve_time_limit=0.001)
    assert "time limit" in rec.text()
    assert result["status"] == "done" and result["placed"] == len(truth.photos)
    scores = evaluate(truth, report, mosaic)
    assert scores["pos_err"] < 20 and 0.9 < scores["coverage"] < 1.1, scores
    # the default limit is far more than a normal section needs
    _, _, _, normal = stitch(truth, tmp_path / "default")
    assert "time limit" not in normal.text()


@covers("workers")
def test_single_worker(sections, tmp_path):
    truth = sections["plain"]
    _, report, mosaic, _ = stitch(truth, tmp_path, workers=1)
    assert_good(evaluate(truth, report, mosaic))


# ----------------------------------------------------------------------------- blending and colour

@covers("blend", "feather_power")
@pytest.mark.parametrize("opts", [
    {"blend": "feather"},
    {"blend": "seam"},
    {"blend": "feather", "feather_power": 1.0},
    {"blend": "feather", "feather_power": 12.0},
    {"blend": "feather", "feather_power": 40.0},
])
def test_blending_covers_the_whole_section(sections, tmp_path, opts):
    """Every blend setting must cover exactly the photos' footprint and match the scene."""
    truth = sections["all"]
    _, report, mosaic, _ = stitch(truth, tmp_path, **opts)
    assert_good(evaluate(truth, report, mosaic))


@covers("gain")
def test_gain_evens_out_exposure(sections, tmp_path):
    truth = sections["gains"]
    _, on, mosaic_on, _ = stitch(truth, tmp_path / "on")
    _, off, mosaic_off, _ = stitch(truth, tmp_path / "off", gain=False)
    assert_good(evaluate(truth, on, mosaic_on))
    assert evaluate(truth, off, mosaic_off)["mae"] > 4, "without it, the exposure steps should show"
    assert all(g == 1 for i in off["images"] for g in i["gain_bgr"])


@covers("flat")
@pytest.mark.parametrize("kind", ["vignette", "gradient"])
def test_flat_removes_uneven_illumination(sections, tmp_path, kind):
    truth = sections[kind]
    _, on, mosaic_on, _ = stitch(truth, tmp_path / "on")
    _, off, mosaic_off, _ = stitch(truth, tmp_path / "off", flat=False)
    assert_good(evaluate(truth, on, mosaic_on))
    assert evaluate(truth, off, mosaic_off)["mae"] > 4
    assert on["illumination_range_pct"] > 15
    assert off["illumination_range_pct"] == 0


@pytest.mark.parametrize("seed", range(4))
def test_flat_does_not_invent_lighting_from_exposure_differences(tmp_path, seed):
    """Regression: exposure differences between photos used to be mistaken for a lighting pattern."""
    truth = make_section(str(tmp_path / "gains"), gains=True, seed=seed)
    _, report, mosaic, _ = stitch(truth, tmp_path / "out")
    assert report["illumination_range_pct"] < 5
    assert_good(evaluate(truth, report, mosaic), mae=2.5)


# ----------------------------------------------------------------------------- input and output

@covers("exclude")
def test_exclude_patterns(sections, tmp_path):
    truth = sections["all"]
    copy = truth.skipped_by_default[0]
    _, default, _, _ = stitch(truth, tmp_path / "default")
    _, nothing, _, _ = stitch(truth, tmp_path / "nothing", exclude=[], preview_only=True)
    _, custom, _, _ = stitch(truth, tmp_path / "custom", exclude=["* - Copy.*", "image0005.*"],
                             preview_only=True)
    names = lambda r: {i["file"] for i in r["images"]}  # noqa: E731
    assert copy not in names(default)
    assert copy in names(nothing)
    assert "image0005.tif" not in names(custom) and "image0004.tif" in names(custom)


@covers("output")
def test_output_files(sections, tmp_path):
    truth = sections["plain"]
    out = tmp_path / "nested" / "results"
    result, report, _, _ = stitch(truth, out)
    name = os.path.basename(truth.folder)
    for suffix in (".tif", "_preview.jpg", "_layout.jpg", "_report.json"):
        assert (out / f"{name}{suffix}").is_file()
    assert not list(out.glob("*.partial"))
    with tifffile.TiffFile(result["outputs"]["tif"]) as tif:
        page = tif.pages[0]
        assert (page.imagewidth, page.imagelength) == (report["width_px"], report["height_px"])
        assert page.photometric == tifffile.PHOTOMETRIC.RGB
        # the resolution tag carries the calibration (pixels per cm)
        x_res = page.tags["XResolution"].value
        assert x_res[0] / x_res[1] == pytest.approx(1e4 / truth.um_per_px, rel=1e-3)


def bar_centre_in_mosaic(truth, report):
    """Where the copy's scale bar should land: its spot in the photo it was copied from (rigidly)."""
    bar = truth.scale_bar
    image = next(i for i in report["images"] if i["file"] == bar["photo"])
    x, y, w, h = bar["box"]
    pw, ph = truth.photo_size
    offset = np.array([x + w / 2 - pw / 2, y + h / 2 - ph / 2])
    th = np.radians(image["rotation_deg"])
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return np.asarray(image["centre_xy"]) + R @ offset


@covers("scale_bar")
def test_scale_bar_from_copy_is_pasted_at_scale(sections, tmp_path):
    truth = sections["all"]
    result, on, mosaic, rec = stitch(truth, tmp_path / "on")
    assert [b["file"] for b in on["scale_bars"]] == truth.skipped_by_default
    bar = on["scale_bars"][0]
    assert bar["photo"] == truth.scale_bar["photo"]
    assert bar["length_px"] == truth.scale_bar["length_px"]
    assert bar["length_um"] == pytest.approx(1000, abs=1)
    x, y, w, h = bar["box_xywh"]
    # a few px of slack: the prediction ignores the lens distortion near the photo's corner
    assert np.hypot(*(np.array([x + w / 2, y + h / 2]) - bar_centre_in_mosaic(truth, on))) < 5
    # the full-resolution mosaic and the preview both carry the bar, the same length as in the photo
    found = find_scale_bar(np.ascontiguousarray(mosaic[y:y + h, x:x + w]))
    assert found is not None and found[4] == truth.scale_bar["length_px"]
    preview = cv2.imread(result["outputs"]["preview"])
    s = on["preview_width_px"] / on["width_px"]
    crop = preview[int(y * s):int((y + h) * s) + 1, int(x * s):int((x + w) * s) + 1].astype(float)
    assert np.abs(np.median(crop.reshape(-1, 3), 0) - [43, 54, 101]).max() < 15  # mostly bar colour
    assert "scale bar from image - Copy.tif" in rec.text()

    _, off, mosaic_off, _ = stitch(truth, tmp_path / "off", scale_bar=False)
    assert off["scale_bars"] == []
    assert find_scale_bar(np.ascontiguousarray(mosaic_off[y:y + h, x:x + w])) is None


def test_scale_bar_at_another_magnification_is_left_out(tmp_path):
    truth = make_section(str(tmp_path / "zoomed-copy"), copy=True)
    write_calibration(os.path.join(truth.folder, "image - Copy.tif"), 8.0, *truth.photo_size)
    _, report, _, rec = stitch(truth, tmp_path / "out", preview_only=True)
    assert report["scale_bars"] == []
    assert "different magnification" in rec.text()


@covers("preview_width")
@pytest.mark.parametrize("width", [300, 100000])
def test_preview_width(sections, tmp_path, width):
    import cv2
    truth = sections["plain"]
    result, report, _, _ = stitch(truth, tmp_path, preview_width=width, preview_only=True)
    preview = cv2.imread(result["outputs"]["preview"])
    assert preview.shape[1] == min(width, report["width_px"])


@covers("preview_only")
def test_preview_only(sections, tmp_path):
    truth = sections["plain"]
    result, report, _, _ = stitch(truth, tmp_path, preview_only=True)
    assert result["status"] == "preview"
    assert not os.path.exists(result["outputs"]["tif"])
    assert os.path.exists(result["outputs"]["preview"]) and os.path.exists(result["outputs"]["layout"])
    assert report["full_resolution_written"] is False


@covers("skip_existing")
def test_skip_existing(sections, tmp_path):
    truth = sections["plain"]
    first, _, _, _ = stitch(truth, tmp_path, skip_existing=True)
    again, _, _, _ = stitch(truth, tmp_path, skip_existing=True)
    redo, _, _, _ = stitch(truth, tmp_path, skip_existing=False, preview_only=True)
    assert first["status"] == "done"
    assert again["status"] == "skipped"
    assert redo["status"] == "preview"
    # a preview alone doesn't count as stitched for a full run
    other = tmp_path / "preview_first"
    stitch(truth, other, preview_only=True)
    full, _, _, _ = stitch(truth, other, skip_existing=True)
    assert full["status"] == "done"


# ----------------------------------------------------------------------------- guard rails

def test_option_coverage():
    """Every setting has a test above that checks its effect, and a non-default test value."""
    names = {f.name for f in fields(Options)}
    assert names - COVERED == set(), "add a test (marked with @covers) for these settings"
    assert COVERED - names == set(), "@covers names a setting that doesn't exist"
    assert names == set(NONDEFAULT), "add these settings to helpers.NONDEFAULT"
    defaults = Options()
    same = [k for k, v in NONDEFAULT.items() if getattr(defaults, k) == v]
    assert not same, f"NONDEFAULT values equal the defaults for {same}"
