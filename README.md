# Shell section stitcher

Automatically stitches the overlapping microscope photos of a shell section into one
full-resolution image for sclerochronology. One folder of photos in, one mosaic out.

## Setup (once)

Needs [uv](https://docs.astral.sh/uv/) (already installed on this machine). It creates the
Python environment automatically on first run.

## Usage

```bash
uv run stitch.py images/M27                # one section
uv run stitch.py images/*/                 # every folder in images/
uv run stitch.py /path/to/new/shells/*/    # any other set of folders
uv run stitch.py images/*/ --skip-existing # only folders not stitched yet
```

For each folder `NAME`, the `stitched/` folder receives:

| File | What it is |
|---|---|
| `NAME.tif` | Full-resolution mosaic (uncompressed RGB TIFF, opens in Fiji/ImageJ, Photoshop, Illustrator). Its resolution tag carries the Leica µm/pixel calibration, so physical size is correct (in Fiji: *Image ▸ Properties* shows the scale). |
| `NAME_preview.jpg` | ~4000 px wide preview for a quick look. |
| `NAME_layout.jpg` | Preview with every photo's outline and number drawn on, for checking placement. |
| `NAME_report.json` | Calibration, alignment error, each photo's position/rotation, and any photos that couldn't be placed. |

A typical folder takes 1–2 minutes.

### What to check

The console prints a line like
`aligned 56/56 images using 186 overlaps, typical error 0.42px`.

- **Typical error** is how far matching points in overlapping photos disagree after
  alignment, in pixels. Below ~1 px (small images) or ~2 px (the 3648 px wide images) is
  normal.
- **could not place …** lists photos with no reliable overlap with the rest. These are
  often stray shots, such as a photo of a different area or with different lighting. Look at
  them before assuming anything is wrong. If a genuine photo is left out, its neighbours
  probably overlap it too little; retaking with more overlap (about 30 %) is the reliable
  fix.

## How it works

1. **Feature matching**: SIFT features are found in every photo (contrast-enhanced so the
   smooth nacre still yields features) and every photo is matched against every other, so
   capture order and scan pattern don't matter.
2. **Global alignment**: all matches are solved together by least squares. The model is
   physical. The sample moves rigidly under a fixed camera (rotation + translation per
   photo, **no scaling**, so distances stay measurable). The camera's slight oblique view and
   lens distortion are shared by every photo and estimated at the same time. Inconsistent
   (false) matches are detected and discarded.
3. **Full-resolution refinement**: each overlap is re-measured at full resolution with
   phase correlation, and the solve is repeated.
4. **Illumination correction**: one smooth illumination pattern shared by all photos
   (uneven lighting or vignetting) and a brightness/colour gain for each photo are estimated
   from the overlaps and divided out. This removes the tile-to-tile brightness steps that
   could otherwise be mistaken for bands in intensity profiles.
5. **Blending**: photos are feathered together near their seams and written in strips, so
   large mosaics don't need much memory.

### Calibration note

LAS writes each photo's calibration to `.Metadata/<name>.eax`, but it records it for the
image size in that file. In M72 and M74 that is 1024 px, although the photos were saved at
3648 px. The script rescales the value to the real width. That gives 3.27 µm/px for those
two sections; the raw metadata says 11.64, which would be wrong. The value used is in the
report and the TIFF.

## Options

```text
--blend seam          near-hard seams instead of smooth feathering (never doubles a line,
                      but exposure steps may show)
--feather-power N     width of feathered transitions: higher = narrower (default 3)
--exclude PATTERN     skip files matching PATTERN (default already skips "* - Copy.*",
                      which are duplicates of image.tif with a burned-in scale bar)
--no-gain             no exposure/illumination correction at all (raw pixel values)
--no-flat             per-photo gains only, no illumination-pattern correction
--no-perspective      rigid alignment only (don't model camera tilt/lens distortion)
--no-refine           skip the full-resolution refinement pass (faster)
--preview-only        only write the preview/layout/report (fast check)
-n, --skip-existing   skip folders whose mosaic already exists in the output folder
                      (delete a mosaic to have it redone)
-o DIR                output folder (default: stitched)
```

Run `uv run stitch.py --help` for the full list.

## License

MIT. See [LICENSE](LICENSE).
