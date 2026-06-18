#!/usr/bin/env python3
"""
Build script — creates a standalone ReverseImageSearch.exe.
Run with: python build.py
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

SRC = Path(__file__).parent


def run(cmd: list, label: str = "") -> None:
    if label:
        print(f"  {label}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nERROR: command failed (exit code {result.returncode})")
        input("Press Enter to exit...")
        sys.exit(1)


def main():
    print("=" * 55)
    print("  Reverse Image Search — Build Standalone .exe")
    print("=" * 55)
    print()

    # ── 1. Install dependencies ───────────────────────────
    print("[1/4] Installing dependencies...")
    run(
        [sys.executable, "-m", "pip", "install",
         "requests", "openpyxl", "Pillow", "pyinstaller"],
    )
    print()

    # ── 2. Stage source files in a local temp folder ──────
    #      (avoids OneDrive / network-path permission issues)
    print("[2/4] Staging source files in local temp folder...")
    build_dir = Path(tempfile.gettempdir()) / "ris_build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    for fname in ("gui.py", "reverse_image_search.py"):
        shutil.copy(SRC / fname, build_dir / fname)

    print(f"  Staged to: {build_dir}")
    print()

    # ── 3. Build ──────────────────────────────────────────
    print("[3/4] Building ReverseImageSearch.exe")
    print("       (this takes 1–3 minutes on first run)...")
    print()

    dist_dir = build_dir / "dist"
    work_dir = build_dir / "work"
    orig_cwd = Path.cwd()
    os.chdir(build_dir)

    run([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "ReverseImageSearch",
        "--hidden-import=openpyxl",
        "--hidden-import=openpyxl.cell._writer",
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.JpegImagePlugin",
        "--hidden-import=PIL.PngImagePlugin",
        "--hidden-import=PIL.GifImagePlugin",
        "--hidden-import=PIL.WebPImagePlugin",
        "--hidden-import=PIL.BmpImagePlugin",
        "--hidden-import=requests",
        "--hidden-import=charset_normalizer",
        "--collect-all", "openpyxl",
        f"--distpath={dist_dir}",
        f"--workpath={work_dir}",
        f"--specpath={build_dir}",
        "--clean",
        "--noconfirm",
        "gui.py",
    ])

    os.chdir(orig_cwd)

    # ── 4. Copy exe back to project folder ────────────────
    print()
    print("[4/4] Copying exe to project folder...")

    out_dir = SRC / "dist"
    out_dir.mkdir(exist_ok=True)
    exe_src = dist_dir / "ReverseImageSearch.exe"
    exe_dst = out_dir / "ReverseImageSearch.exe"
    shutil.copy(exe_src, exe_dst)

    print()
    print("=" * 55)
    print("  Done!")
    print()
    print(f"  {exe_dst}")
    print()
    print("  Send this .exe to anyone on Windows.")
    print("  No Python or other software required.")
    print("=" * 55)

    os.startfile(str(out_dir))
    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
