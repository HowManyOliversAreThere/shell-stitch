# PyInstaller build for the Shell Stitch desktop app.
#   uv run --group build pyinstaller --noconfirm packaging/shellstitch.spec
# Produces dist/ShellStitch/ (Windows, Linux) or dist/ShellStitch.app (macOS).
# Verify a build with:  <executable> --self-test

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(SPECPATH, "..")))
from shellstitch import __version__  # noqa: E402

root = os.path.abspath(os.path.join(SPECPATH, ".."))
icon = os.path.join(root, "shellstitch", "gui", "resources", "icon.png")

a = Analysis(
    [os.path.join(SPECPATH, "shellstitch_gui.py")],
    pathex=[root],
    datas=[(os.path.join(root, "shellstitch", "gui", "resources"), "shellstitch/gui/resources"),
           (os.path.join(root, "LICENSE"), ".")],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShellStitch",
    console=False,
    icon=icon,
    # PyInstaller's argv emulation is only needed for opening files via Finder
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ShellStitch")
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ShellStitch.app",
        icon=icon,
        bundle_identifier="io.github.shellstitch",
        version=__version__,
        info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0",
                    "CFBundleDisplayName": "Shell Stitch"},
    )
