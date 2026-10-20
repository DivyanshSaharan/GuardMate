"""Write a portable ZIP of an existing verified Windows runtime."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]


def create_archive(runtime: Path, output: Path):
    runtime, output = runtime.resolve(), output.resolve()
    if not runtime.is_relative_to(ROOT) or not output.is_relative_to(ROOT):
        raise ValueError("Runtime and archive must be inside the workspace.")
    if not (runtime / "GuardMate.exe").is_file():
        raise ValueError("Use a completed GuardMate runtime.")
    # Exclusive creation preserves release immutability.
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(runtime.rglob("*")):
            if path.is_symlink():
                raise ValueError("Runtime must not contain symbolic links.")
            archive.write(path, arcname=path.relative_to(runtime).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    create_archive(args.runtime, args.output)


if __name__ == "__main__":
    main()
