"""Command line: stitch one or more folders of overlapping photos.

Usage:
    uv run stitch.py images/M27                 # one folder
    uv run stitch.py images/*/                  # several folders
    uv run stitch.py images/*/ --skip-existing  # only folders not stitched yet

Press Ctrl-C once to skip the section being stitched, twice quickly to stop.
"""

import argparse
import time

from tqdm import tqdm

from .engine import Cancelled, Reporter, SkipSection, stitch_folder
from .options import Options, default_of, option_fields


class CliReporter(Reporter):
    """Prints log lines, a progress bar per stage, and a live status line for solver stages."""

    def __init__(self):
        super().__init__()
        self._status = None

    def _close_status(self):
        if self._status is not None:
            self._status.close()
            self._status = None

    def track(self, iterable, total, desc):
        self._close_status()
        fmt = "{desc:<32}{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"
        with tqdm(total=total, desc=f"    {desc}", ncols=100, bar_format=fmt) as bar:
            for item in iterable:
                yield item
                bar.update()

    def busy(self, desc):
        super().busy(desc)
        self._close_status()
        self._status = tqdm(total=None, desc=f"    {desc}", ncols=100,
                            bar_format="{desc:<32}{postfix} [{elapsed}]")

    def tick(self, detail):
        super().tick(detail)
        if self._status is not None:
            self._status.set_postfix_str(detail, refresh=True)

    def section(self, name):
        super().section(name)
        self._close_status()

    def log(self, msg, level="info"):
        tqdm.write(msg)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="folders of overlapping images (one mosaic per folder)")
    for f in option_fields():
        m = f.metadata
        flags = m["cli"].split("/")
        default = default_of(f)
        help_ = m["help"]
        if isinstance(default, bool):
            if default:  # on by default: the flag turns it off
                help_ = f"Turn off '{m['label']}'. {help_}"
            action = "store_false" if default else "store_true"
            ap.add_argument(*flags, dest=f.name, action=action, help=help_)
        elif isinstance(default, list):
            ap.add_argument(*flags, dest=f.name, action="append", default=None, metavar="PATTERN",
                            help=f"{help_} Repeatable; adds to the defaults {default}.")
        else:
            kw = {"type": type(default), "default": default}
            if "choices" in m:
                kw["choices"] = m["choices"]
            ap.add_argument(*flags, dest=f.name, help=f"{help_} (default: {default})", **kw)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    opts = Options()
    for f in option_fields():
        v = getattr(args, f.name)
        if isinstance(default_of(f), list):
            v = default_of(f) + (v or [])
        setattr(opts, f.name, v)
    rep = CliReporter()
    failed = []
    last_interrupt = 0.0
    for folder in args.folders:
        try:
            stitch_folder(folder, opts, rep)
        except (KeyboardInterrupt, SkipSection):
            rep._close_status()
            if time.monotonic() - last_interrupt < 3:
                raise SystemExit("stopped") from None
            last_interrupt = time.monotonic()
            rep.log(f"[{folder}] skipped (press Ctrl-C again within 3 s to stop everything)")
        except Cancelled:
            raise SystemExit("cancelled") from None
        except Exception as e:  # keep going with the other folders
            rep.log(f"[{folder}] FAILED: {e}")
            failed.append(folder)
            if len(args.folders) == 1:
                raise
    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
