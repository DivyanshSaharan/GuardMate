"""Upload common, credential-free artifacts to private R2; inspect by default."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.cloud.downloads import ENVELOPE_MAGIC, R2Downloads, Release  # noqa: E402


def describe(path, version, key):
    path = path.resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("Use a build artifact inside the workspace.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return Release(version, key, digest.hexdigest(), path.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument(
        "--upload", action="store_true", help="Upload using explicit environment keys"
    )
    args = parser.parse_args()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", args.version) is None:
        parser.error("Use a semantic release version.")
    runtime = describe(args.runtime, args.version, f"releases/{args.version}/runtime.zip")
    installer = describe(
        args.installer, args.version, f"releases/{args.version}/setup.template.exe"
    )
    content = args.installer.read_bytes()
    if not content.startswith(b"MZ") or content.endswith(ENVELOPE_MAGIC):
        parser.error("Upload the unsigned common template, never a personalized installer.")
    result = {
        "GUARDMATE_DESKTOP_RELEASE": vars(runtime),
        "GUARDMATE_INSTALLER_RELEASE": vars(installer),
    }
    if args.upload:
        from botocore.exceptions import ClientError

        downloads = R2Downloads.from_credentials(
            endpoint=os.environ["GUARDMATE_R2_ENDPOINT"],
            bucket=os.environ["GUARDMATE_R2_BUCKET"],
            access_key=os.environ["GUARDMATE_UPLOAD_R2_ACCESS_KEY_ID"],
            secret_key=os.environ["GUARDMATE_UPLOAD_R2_SECRET_ACCESS_KEY"],
            release=runtime,
            installer=installer,
        )
        for path, release in ((args.runtime, runtime), (args.installer, installer)):
            try:
                downloads.client.head_object(Bucket=downloads.bucket, Key=release.key)
            except ClientError as error:
                if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
                with path.open("rb") as source:
                    downloads.client.put_object(
                        Bucket=downloads.bucket,
                        Key=release.key,
                        Body=source,
                        ContentLength=release.size,
                        Metadata={"sha256": release.sha256},
                        ContentType="application/octet-stream",
                        IfNoneMatch="*",
                    )
            response = downloads.client.get_object(Bucket=downloads.bucket, Key=release.key)
            body, digest, size = response["Body"], hashlib.sha256(), 0
            try:
                for block in iter(lambda current=body: current.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    if size > release.size:
                        raise ValueError("Artifact verification failed.")
            finally:
                body.close()
            if size != release.size or digest.hexdigest() != release.sha256:
                raise ValueError("Artifact verification failed.")
        destination = ROOT / ".cache/windows-release/render-release.json"
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"uploaded": args.upload, **result}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Publication failed. Check explicit upload credentials, artifact paths and version. "
            "Existing release keys are never overwritten."
        ) from None
