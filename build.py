#!/usr/bin/env python3
"""
Cross-platform build script — packages Mean Reversion Trader into a single executable.

PyInstaller does NOT cross-compile: run this script on the target OS.
  macOS   →  dist/mean_reversion_trader      (or dist/mean_reversion_trader_cli)
  Linux   →  dist/mean_reversion_trader      (or dist/mean_reversion_trader_cli)
  Windows →  dist/mean_reversion_trader.exe  (or dist/mean_reversion_trader_cli.exe)

Usage:
    python build.py          # build the GUI version  (gui.py)
    python build.py --cli    # build the CLI version  (main.py)
"""

import argparse
import os
import subprocess
import sys


# ── Hidden imports that PyInstaller's static analysis can miss ───────────────
_COMMON_HIDDEN = [
    # colorama
    "colorama",
    "colorama.initialise",
    "colorama.ansitowin32",
    "colorama.winterm",
    # websocket-client internals
    "websocket",
    "websocket._abnf",
    "websocket._core",
    "websocket._exceptions",
    "websocket._handshake",
    "websocket._http",
    "websocket._logging",
    "websocket._socket",
    "websocket._ssl_compat",
    "websocket._utils",
    # tqdm
    "tqdm",
    "tqdm.auto",
    "tqdm.std",
    # setuptools
    "pkg_resources",
    "pkg_resources.py2_warn",
]

_GUI_HIDDEN = _COMMON_HIDDEN + [
    # customtkinter
    "customtkinter",
    "customtkinter.windows",
    "customtkinter.windows.widgets",
    "customtkinter.windows.widgets.appearance_mode",
    "customtkinter.windows.widgets.scaling",
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
]

# ── Data files to bundle  (source_path, dest_dir_inside_bundle) ──────────────
_DATA = [
    (
        os.path.join("engine", "config", "default_config.json"),
        os.path.join("engine", "config"),
    ),
]


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found — installing…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller>=5.0"],
            check=True,
        )


def _build(gui: bool) -> None:
    _ensure_pyinstaller()

    sep      = os.pathsep   # ':' on Unix, ';' on Windows
    entry    = "gui.py"   if gui else "main.py"
    exe_name = "mean_reversion_trader" if gui else "mean_reversion_trader_cli"
    hidden   = _GUI_HIDDEN  if gui else _COMMON_HIDDEN

    hidden_flags = []
    for h in hidden:
        hidden_flags += ["--hidden-import", h]

    data_flags = []
    for src, dst in _DATA:
        data_flags += ["--add-data", f"{src}{sep}{dst}"]

    # customtkinter bundles theme JSON / image files — collect them all
    collect_flags = []
    if gui:
        collect_flags = ["--collect-data", "customtkinter"]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed" if gui else "--console",
        "--name", exe_name,
        "--clean",
        *hidden_flags,
        *data_flags,
        *collect_flags,
        entry,
    ]

    mode = "GUI" if gui else "CLI"
    print("=" * 62)
    print(f"  Mean Reversion Trader — building {mode} executable")
    print(f"  Entry    : {entry}")
    print(f"  Platform : {sys.platform}")
    print(f"  Python   : {sys.version.split()[0]}")
    print("=" * 62)
    print()

    subprocess.run(cmd, check=True)

    output = os.path.join("dist", exe_name)
    if sys.platform == "win32":
        output += ".exe"

    print()
    print("=" * 62)
    print("  Build complete!")
    print(f"  Output : {output}")
    print()
    print("  The executable is fully self-contained.")
    print("  Trade logs are written to  data/  next to the exe.")
    if not gui:
        print()
        print("  Run:")
        run = output if sys.platform == "win32" else f"./{output}"
        print(f"    {run}")
        print(f"    {run} --symbols XRPUSDT")
    print("=" * 62)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Mean Reversion Trader executable")
    parser.add_argument("--cli", action="store_true",
                        help="Build the headless CLI version instead of the GUI")
    args = parser.parse_args()
    _build(gui=not args.cli)
