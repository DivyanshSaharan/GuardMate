"""Private R2 artifacts and per-account installer envelopes; no credentials in public assets."""

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

from .store import CloudError

ENVELOPE_MAGIC = b"GMATE-ENVELOPE1!"


def personalize_installer(template: bytes, public_origin: str, credential: str) -> bytes:
    origin = urlsplit(public_origin)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path
        or not 20 <= len(credential) <= 4096
        or not template.startswith(b"MZ")
    ):
        raise CloudError("installer_unavailable", 503)
    envelope = json.dumps(
        {"version": 1, "server_url": public_origin, "bootstrap_token": credential},
        separators=(",", ":"),
    ).encode()
    if len(envelope) > 16_384:
        raise CloudError("installer_unavailable", 503)
    return (
        template
        + envelope
        + hashlib.sha256(envelope).digest()
        + struct.pack("<I", len(envelope))
        + ENVELOPE_MAGIC
    )


@dataclass(frozen=True)
class Release:
    version: str
    key: str
    sha256: str
    size: int

    def __post_init__(self):
        if (
            not self.version
            or not self.key
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
            or type(self.size) is not int
            or not 0 < self.size <= 4_000_000_000
        ):
            raise ValueError("Invalid desktop release metadata.")


class R2Downloads:
    def __init__(self, client, bucket, release: Release, installer: Release):
        self.client, self.bucket = client, bucket
        self.release, self.installer = release, installer

    @classmethod
    def from_credentials(cls, *, endpoint, access_key, secret_key, bucket, release, installer):
        import boto3
        from botocore.config import Config

        if not re.fullmatch(r"https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com", endpoint):
            raise ValueError("Invalid private artifact endpoint.")
        if not all(isinstance(value, str) and value for value in (access_key, secret_key, bucket)):
            raise ValueError("Private artifact credentials are required.")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=20,
                retries={"max_attempts": 0},
            ),
        )
        return cls(client, bucket, release, installer)

    def manifest(self):
        url = self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self.release.key}, ExpiresIn=300
        )
        return {
            "version": self.release.version,
            "url": url,
            "sha256": self.release.sha256,
            "size": self.release.size,
        }

    def personalized(self, origin, credential):
        if self.installer.size > 20_000_000:
            raise CloudError("installer_unavailable", 503)
        body = self.client.get_object(Bucket=self.bucket, Key=self.installer.key)["Body"]
        try:
            content = body.read(self.installer.size + 1)
        finally:
            body.close()
        if (
            len(content) != self.installer.size
            or hashlib.sha256(content).hexdigest() != self.installer.sha256
        ):
            raise CloudError("installer_integrity_failed", 503)
        return personalize_installer(content, origin, credential)
