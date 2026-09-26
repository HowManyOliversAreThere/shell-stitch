# Shell Stitch

Automatically stitches the overlapping microscope photos of a shell section into one
full-resolution, calibrated image for sclerochronology. One folder of photos in, one mosaic
out. It comes as a desktop app for Windows, macOS and Linux, and as a command-line tool.

## Desktop app

### Download

Ready-built apps are attached to each [GitHub release](https://github.com/HowManyOliversAreThere/shell-stitch/releases) (built by
[`.github/workflows/build.yml`](.github/workflows/build.yml)): unzip and run **ShellStitch**.
The builds aren't code-signed, so the first launch needs one extra step:

- **macOS**: right-click *ShellStitch.app* and choose *Open*, then *Open* again. If macOS still
  refuses, run `xattr -dr com.apple.quarantine ShellStitch.app` in Terminal.
- **Windows**: on the SmartScreen warning, choose *More info* and then *Run anyway*.

> [!NOTE]
> **The Linux build hasn't been tested on a real desktop yet.** CI builds it and runs the
> automated self-test, but nobody has used it interactively. If you try it, please
> [open an issue](https://github.com/HowManyOliversAreThere/shell-stitch/issues) to say whether
> it works, with your distribution and version, and any problems.

### Using it

1. **Stitch** page: choose the *photos folder*, which is the folder holding one subfolder of
   overlapping photos per section, and an *output folder*.
2. Tick the sections to stitch. *Not yet stitched* selects only the new ones.
3. Adjust settings if needed. Every setting has a tooltip; *Show advanced settings* reveals
   the rest.
4. *Start stitching*. Progress is shown for every stage, including live solver status.
   *Skip section* abandons a section that is taking too long and moves on; *Cancel* stops.
5. **Results** page: browse the mosaics (scroll to zoom, drag to pan, with a scale bar and a
   cursor position in mm). Toggle the photo outlines, and read each section's report:
   - a summary with an alignment verdict and warnings
   - per-photo and per-overlap tables
   - *Export report…* saves the report as an HTML page

**Preferences** switches the theme between *System* (follows your computer's light or dark
mode), *Light* and *Dark*. Settings, folders and the theme are remembered between sessions.

## Running from source

Needs [uv](https://docs.astral.sh/uv/). It creates the Python environment automatically.

```bash
uv run shellstitch-gui                     # the desktop app
uv run stitch.py images/M27                # command line: one section
uv run stitch.py images/*/                 # every folder in images/
uv run stitch.py images/*/ --skip-existing # only folders not stitched yet
```

On the command line, press Ctrl-C once to skip the section being stitched, and twice quickly
to stop. `uv run stitch.py --help` lists every option; they are the same settings as in the
app.

## Outputs

For each section `NAME`, the output folder receives:

| File | What it is |
|---|---|
| `NAME.tif` | Full-resolution mosaic (uncompressed RGB TIFF, opens in Fiji/ImageJ, Photoshop, Illustrator). Its resolution tag carries the Leica µm/pixel calibration, so physical size is correct (in Fiji: *Image ▸ Properties* shows the scale). |
| `NAME_preview.jpg` | ~4000 px wide preview for a quick look. |
| `NAME_layout.jpg` | Preview with every photo's outline and number drawn on, for checking placement. |
| `NAME_report.json` | Calibration, alignment quality, each photo's position/rotation, photos that were left out and why, and the settings used. The app shows it in readable form. |

A typical section takes under a minute to a few minutes, depending on the number of photos.

### What to check

- **Typical misalignment** is how far matching points in overlapping photos disagree after
  alignment. Below ~1 px (1024 px wide photos) or ~2 px (3648 px wide photos) is normal. The
  app rates it for you.
- **Photos left out**:
  - *No reliable overlap*: often a stray shot, such as a different area or lighting. If a
    genuine photo is left out, retake it with more overlap (about 30 %).
  - *Different magnification or image size*: the photos were taken at another zoom setting
    and can't be combined at the same scale. Stitch them separately in their own folder.

## How it works

1. **Feature matching**: SIFT features are found in every photo (contrast-enhanced so the
   smooth nacre still yields features) and every photo is matched against every other, so
   capture order and scan pattern don't matter.
2. **Camera-fixed features are ignored**: dust or scratches on the optics or stage glass and
   light-source glare stay put while the sample moves. Wherever features match at zero offset
   across several photo pairs, they are discarded. Otherwise they pull photos together
   wrongly, which matters especially in transmitted light.
3. **Global alignment**: all matches are solved together by least squares. The model is
   physical. The sample moves rigidly under a fixed camera (rotation + translation per
   photo, **no scaling**, so distances stay measurable). The camera's slight oblique view and
   lens distortion are shared by every photo and estimated at the same time. Inconsistent
   (false) matches are detected and discarded. Each solve has a time limit, so a difficult
   section can't hold up a batch.
4. **Full-resolution refinement**: each overlap is re-measured at full resolution with
   phase correlation, and the solve is repeated.
5. **Illumination correction**: one smooth illumination pattern shared by all photos
   (uneven lighting or vignetting) and a brightness/colour gain for each photo are estimated
   from the overlaps and divided out. This removes the tile-to-tile brightness steps that
   could otherwise be mistaken for bands in intensity profiles.
6. **Blending**: photos are feathered together near their seams and written in strips, so
   large mosaics don't need much memory.

### Calibration note

LAS writes each photo's calibration to `.Metadata/<name>.eax`, but for the image size recorded
in that file, which isn't always the size the photo was saved at. For example, it can record
a 1024 px calibration on a 3648 px capture. Shell Stitch rescales the value to the real width;
the value used is in the report and the TIFF.

## Development

| Path | Contents |
|---|---|
| `shellstitch/engine.py` | Stitching engine (matching, alignment, illumination, blending). |
| `shellstitch/options.py` | Every setting, defined once; the CLI flags and the app's settings form are generated from it. |
| `shellstitch/report.py` | Human-readable interpretation of `NAME_report.json`. |
| `shellstitch/cli.py` | Command line. `stitch.py` is a shortcut to it. |
| `shellstitch/gui/` | Desktop app (PySide6 / Qt 6). |
| `shellstitch/synthetic.py` | Synthetic sections with a known ground truth, for the tests and the self-test. |
| `shellstitch/selftest.py` | Quick end-to-end check, also built into the app: `uv run python -m shellstitch.selftest --gui`. |
| `tests/` | Test suite (`uv run pytest`). |
| `packaging/` | PyInstaller build and icon source. |

### Tests

```bash
uv run pytest
```

The tests run the real engine on **synthetic sections with a known ground truth**, generated
in code by [`shellstitch/synthetic.py`](shellstitch/synthetic.py), so no real sample images
are needed or included. The synthetic photos are cut from a procedurally generated "shell" at
known positions and rotations. Optionally they add the imperfections the settings exist to
correct: camera tilt and lens distortion, exposure differences, vignetting or one-sided
lighting, dust on the optics, a burned-in-scale-bar duplicate, and a photo at a different
magnification.

Each setting is checked by its *effect*, not just that it runs:
- photos are placed within half a pixel of the truth
- the mosaic covers exactly the photos' footprint
- the mosaic matches the true scene pixel for pixel
- turning a correction off makes the result it's responsible for measurably worse

`test_option_coverage` fails if a setting is added without such a test. The command line and
the settings form are checked to pass every setting through unchanged.

[CI](.github/workflows/ci.yml) lints the code (`uv run ruff check .`) and runs the tests and
the self-test on Windows, macOS and Linux for every pull request and push to `main`. Release
builds only run once those checks pass.

Build the standalone app for the current platform (PyInstaller can't cross-compile, so CI
builds each platform on its own runner):

```bash
uv run --group build pyinstaller --noconfirm packaging/shellstitch.spec
```

The result is in `dist/`. Check a build with `ShellStitch --self-test`: it stitches synthetic
photos with the packaged engine and exits with 0 on success. To publish a release, push a
version tag (e.g. `git tag v0.2.0 && git push --tags`). The workflow builds, self-tests and
attaches Windows, macOS and Linux downloads.

## License

MIT. See [LICENSE](LICENSE). The app bundles open-source components under their own
licenses, listed on its About page:
- Qt/PySide6: LGPL-3.0, dynamically linked, so the Qt libraries can be replaced
- OpenCV: Apache-2.0
- NumPy, SciPy and tifffile: BSD
- tqdm: MPL-2.0/MIT
