"""Immutable, content-addressed storage for raw source payloads (S3 / MinIO).

Objects are keyed by the SHA-256 of their bytes, so a stored payload can never be replaced
by different content under the same key, and every read is verified against its hash.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from patsquire_plr.config import ObjectStorageSettings
from patsquire_plr.errors import PlrError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

HTTP_NOT_FOUND = "404"


class RawStoreError(PlrError):
    """A raw payload is missing or does not match its recorded hash."""


def object_key(source_id: str, sha256: str) -> str:
    return f"raw/{source_id}/{sha256[:2]}/{sha256}"


class RawStore:
    def __init__(self, settings: ObjectStorageSettings, *, client: S3Client | None = None) -> None:
        self._bucket = settings.bucket
        self._client: S3Client = client or boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id.get_secret_value(),
            aws_secret_access_key=settings.secret_access_key.get_secret_value(),
            config=BotoConfig(
                connect_timeout=settings.connect_timeout_s,
                read_timeout=settings.read_timeout_s,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def put(self, source_id: str, content: bytes, content_type: str) -> tuple[str, str]:
        """Store ``content``; returns (object key, sha256). Storing identical bytes twice is a
        no-op; the existing object is verified rather than overwritten."""
        sha256 = hashlib.sha256(content).hexdigest()
        key = object_key(source_id, sha256)
        if self._exists(key):
            self.get(key, sha256)  # verifies the stored bytes
            return key, sha256
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            Metadata={"sha256": sha256, "source-id": source_id},
        )
        return key, sha256

    def get(self, key: str, expected_sha256: str) -> bytes:
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            raise RawStoreError(f"raw payload {key} cannot be read: {exc}") from exc
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected_sha256:
            raise RawStoreError(
                f"raw payload {key} is corrupt: sha256 {actual} != recorded {expected_sha256}"
            )
        return body

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (HTTP_NOT_FOUND, "NoSuchKey"):
                return False
            raise
        return True
