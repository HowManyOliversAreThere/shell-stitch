"""Every stitching setting, defined once.

The command-line flags and the GUI settings form are both generated from `Options`, so the
two always offer the same settings with the same defaults and help text.
"""

import os
from dataclasses import MISSING, asdict, dataclass, field, fields


def _opt(default, label, help, group, **meta):
    meta.update(label=label, help=help, group=group)
    if isinstance(default, list):
        return field(default_factory=lambda: list(default), metadata=meta)
    if callable(default):
        return field(default_factory=default, metadata=meta)
    return field(default=default, metadata=meta)


GROUPS = ["Input", "Alignment", "Blending and colour", "Output", "Performance"]


@dataclass
class Options:
    # Input
    exclude: list[str] = _opt(
        ["* - Copy.*"], "Skip files matching",
        "Filename patterns to ignore (wildcards allowed). The default skips the '* - Copy' "
        "duplicates that have a burned-in scale bar.",
        "Input", cli="--exclude", kind="patterns")

    # Alignment
    perspective: bool = _opt(
        True, "Correct camera tilt and lens distortion",
        "Model the microscope camera's slight oblique view and lens distortion (shared by all "
        "photos). Turn off for rigid alignment only.",
        "Alignment", cli="--no-perspective")
    refine: bool = _opt(
        True, "Refine at full resolution",
        "Re-measure every overlap at full resolution after the initial alignment. Slower but "
        "more accurate.",
        "Alignment", cli="--no-refine")
    match_width: int = _opt(
        1600, "Matching width (px)",
        "Photos are downscaled to this width to find matching features.",
        "Alignment", cli="--match-width", min=400, max=8000, step=100, advanced=True)
    features: int = _opt(
        12000, "Features per photo",
        "Maximum number of SIFT features found in each photo. More helps plain, featureless "
        "areas match but is slower.",
        "Alignment", cli="--features", min=500, max=100000, step=500, advanced=True)
    min_inliers: int = _opt(
        8, "Matches to accept an overlap",
        "How many consistent feature matches two photos need before they are treated as "
        "overlapping.",
        "Alignment", cli="--min-inliers", min=4, max=200, advanced=True)
    ransac_px: float = _opt(
        2.0, "Match tolerance (px)",
        "How far (at matching width) a feature match may deviate and still count as consistent.",
        "Alignment", cli="--ransac-px", min=0.5, max=20.0, step=0.5, decimals=1, advanced=True)

    solve_time_limit: float = _opt(
        120.0, "Solver time limit (s)",
        "Longest a single alignment solve may run before the best solution so far is used, so a "
        "difficult section can't hold up a batch.",
        "Alignment", cli="--solve-time-limit", min=5.0, max=3600.0, step=10.0, decimals=0, advanced=True)

    # Blending and colour
    blend: str = _opt(
        "feather", "Blending",
        "feather: smooth transitions between photos. seam: near-hard seams that never double "
        "a line, though exposure steps may show.",
        "Blending and colour", cli="--blend", choices=["feather", "seam"])
    feather_power: float = _opt(
        3.0, "Feather sharpness",
        "Higher values make the transition between overlapping photos narrower.",
        "Blending and colour", cli="--feather-power", min=1.0, max=40.0, step=0.5, decimals=1,
        advanced=True)
    gain: bool = _opt(
        True, "Match exposure between photos",
        "Even out brightness and colour differences between photos. Turn off to keep raw "
        "pixel values.",
        "Blending and colour", cli="--no-gain")
    flat: bool = _opt(
        True, "Correct uneven illumination",
        "Remove the lighting pattern shared by all photos (vignetting, one-sided lighting), "
        "which otherwise shows as brightness steps between tiles.",
        "Blending and colour", cli="--no-flat")

    # Output
    output: str = _opt(
        "stitched", "Output folder",
        "Where mosaics, previews and reports are written.",
        "Output", cli="-o/--output", kind="dir", gui=False)
    preview_width: int = _opt(
        4000, "Preview width (px)",
        "Width of the preview and layout JPEGs.",
        "Output", cli="--preview-width", min=500, max=20000, step=500)
    preview_only: bool = _opt(
        False, "Preview only (no full-resolution TIFF)",
        "Only write the preview, layout and report. Useful for a quick check.",
        "Output", cli="--preview-only")
    skip_existing: bool = _opt(
        False, "Skip sections that are already stitched",
        "Leave out folders that already have a mosaic in the output folder.",
        "Output", cli="-n/--skip-existing")

    # Performance
    workers: int = _opt(
        lambda: os.cpu_count() or 4, "Parallel threads",
        "How many CPU threads to use.",
        "Performance", cli="--workers", min=1, max=256, advanced=True)

    @classmethod
    def from_dict(cls, data):
        """Build from saved settings, ignoring unknown or invalid entries."""
        opts = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            v = data[f.name]
            default = getattr(opts, f.name)
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, float):
                    v = float(v)
                elif isinstance(default, list):
                    v = [str(x) for x in v]
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            if "choices" in f.metadata and v not in f.metadata["choices"]:
                continue
            setattr(opts, f.name, v)
        return opts

    def to_dict(self):
        return asdict(self)

    def changed_from_default(self):
        """Settings that differ from their defaults: {name: value}."""
        base = Options()
        return {k: v for k, v in asdict(self).items() if getattr(base, k) != v}


def option_fields():
    """Fields in display order with their metadata."""
    return [f for f in fields(Options)]


def default_of(f):
    if f.default is not MISSING:
        return f.default
    return f.default_factory()
