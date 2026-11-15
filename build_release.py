"""Build the Forever Young preview from a local NX2navi installation."""

from pathlib import Path
from shutil import copytree
from subprocess import run
import sys
from tempfile import TemporaryDirectory

from build_voices import ROOT, read_range


def main():
    output = ROOT / "release" / "Forever Young"
    if output.exists():
        raise SystemExit(f"Move the previous build out of {output} before rebuilding.")
    dictionary = ROOT / "assets" / "ZsDic.pack.zs"
    dictionary.parent.mkdir(exist_ok=True)
    dictionary.write_bytes(read_range("Pack/ZsDic.pack.zs"))
    run([sys.executable, str(ROOT / "build_voices.py")], cwd=ROOT, check=True)
    run([sys.executable, str(ROOT / "build_child_animation_test.py"), "--native-child-model"],
        cwd=ROOT, check=True)
    output.parent.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as work:
        staging = Path(work) / "Forever Young"
        sound = ROOT / "Forever Young" / "romfs" / "Sound"
        native = ROOT / "Forever Young Native Child Model Test" / "romfs"
        copytree(sound, staging / "romfs" / "Sound")
        for folder in ("AS", "Pack"):
            copytree(native / folder, staging / "romfs" / folder)
        count = sum(file.is_file() for file in staging.rglob("*"))
        if count != 334:
            raise SystemExit(f"Expected 334 preview files, found {count}.")
        staging.rename(output)
    print(f"Preview ready in {output}")


if __name__ == "__main__":
    main()
