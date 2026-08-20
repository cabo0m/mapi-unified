from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the unified MAPI Windows release bundle")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist-windows")
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        wheel_dir = Path(td) / "wheel"
        wheel_dir.mkdir()
        built = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)],
            check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if built.returncode != 0:
            raise RuntimeError("wheel_build_failed:" + built.stderr[-2000:])
        wheels = list(wheel_dir.glob("mapi_agent_memory-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected_one_wheel:{len(wheels)}")
        wheel = wheels[0]
        checksum_lines = [f"{sha256(wheel)}  {wheel.name}"]
        bundle_root = Path(td) / "bundle"
        bundle_root.mkdir()
        shutil.copy2(wheel, bundle_root / wheel.name)
        for name in ("install-windows.ps1", "uninstall-windows.ps1", "LICENSE", "README.md"):
            shutil.copy2(ROOT / name, bundle_root / name)
        (bundle_root / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
        version = wheel.name.removeprefix("mapi_agent_memory-").split("-", 1)[0]
        zip_path = output / f"MAPI-Windows-{version}.zip"
        with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(bundle_root.iterdir()):
                archive.write(path, path.name)
        sidecar = zip_path.with_suffix(zip_path.suffix + ".sha256")
        sidecar.write_text(f"{sha256(zip_path)}  {zip_path.name}\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "bundle": str(zip_path), "sha256": sha256(zip_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
