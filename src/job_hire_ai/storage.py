import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from job_hire_ai.errors import ATSError


class LocalStorage:
    def __init__(self, directory: Path, max_bytes: int):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def validate_size(self, data: bytes):
        if not data:
            raise ATSError("The file is empty.")
        if len(data) > self.max_bytes:
            raise ATSError(f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit.")

    @staticmethod
    def checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}\.(pdf|docx)", key):
            raise ATSError("Invalid storage key.")
        target = self.directory / key
        if target.resolve().parent != self.directory or target.is_symlink():
            raise ATSError("Unsafe storage path.")
        return target

    def save(self, data: bytes, extension: str) -> str:
        self.validate_size(data)
        key = f"{uuid4().hex}.{extension}"
        target = self.path(key)
        # Open exclusively BEFORE cleanup is enabled: a collision must never delete
        # a previously stored file whose ownership belongs to another transaction.
        handle = target.open("xb")
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return key

    def retrieve(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as exc:
            raise ATSError(
                "The stored file is missing. Restore it from your local backup."
            ) from exc

    def delete(self, key: str):
        self.path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()
