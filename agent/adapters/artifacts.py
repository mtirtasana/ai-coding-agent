"""Workspace-bound receipts. Disk storage is opt-in and always authenticated/encrypted."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import threading
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from .user_paths import user_directory


MAX_ARTIFACT = 8 * 1024 * 1024
MAX_STORAGE = 64 * 1024 * 1024
TTL = 30 * 86400


def receipt_view(receipt):
    """Small model-facing reference; authenticated metadata is recoverable by handle."""
    return {
        key: receipt[key]
        for key in ("version", "handle", "kind", "bytes", "sha256", "persistent")
    }


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def policy_digest(workspace):
    path = workspace.root / ".agentignore"
    if path.is_symlink():
        raise ValueError("Invalid archive access policy.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        raw = b""
    if len(raw) > 65536:
        raise ValueError("Invalid archive access policy.")
    raw.decode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ArtifactSetupError(ValueError):
    pass


class ArtifactStore:
    def __init__(
        self, workspace, *, persistent=False, directory=None, key=None, clock=time.time
    ):
        try:
            self._initialize(workspace, persistent, directory, key, clock)
        except (OSError, ValueError, ImportError):
            raise ArtifactSetupError(
                "Artifact storage unavailable. Install .[archives], use a state directory outside the workspace, and unlock the OS vault or configure a valid AGENT_ARCHIVE_KEY. No plaintext fallback was used."
            ) from None

    def _initialize(self, workspace, persistent, directory, key, clock):
        self.workspace = workspace
        self.scope = hashlib.sha256(str(workspace.root).encode()).hexdigest()
        self.persistent, self.clock = persistent, clock
        self.directory = (
            Path(directory)
            if directory is not None
            else (
                user_directory("state") if persistent else Path(tempfile.gettempdir())
            )
            / "artifacts"
            / self.scope
        )
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.cipher = None
        if persistent:
            if self.directory.resolve().is_relative_to(workspace.root):
                raise ValueError("Artifact storage must be outside the workspace.")
            self._directory()
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError:
                raise ValueError(
                    "Persistent artifacts require the .[archives] extra."
                ) from None
            key = key if key is not None else self._vault_key()
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("Artifact encryption requires a 32-byte key.")
            self.cipher = AESGCM(key)
        self.key = key if key is not None else secrets.token_bytes(32)
        if not isinstance(self.key, bytes) or len(self.key) != 32:
            raise ValueError("Artifact authentication requires a 32-byte key.")
        self.auth_key = hmac.digest(self.key, b"agent-artifact-receipt-v1", "sha256")

    def _directory(self):
        # Reject symlinked roots and ancestors before accessing ciphertext or locks.
        cursor = self.directory.absolute()
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise ValueError("Artifact storage must not use symlinks.")
            cursor = cursor.parent
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    def _vault_key(self):
        configured = os.getenv("AGENT_ARCHIVE_KEY")
        if configured is not None:
            try:
                return base64.b64decode(configured, validate=True)
            except ValueError:
                raise ValueError(
                    "AGENT_ARCHIVE_KEY must be base64 for 32 random bytes."
                ) from None
        from .connections import OSVault

        # Serialize initial key creation across workers; no plaintext key file.
        lock = self.directory / ".key-init.lock"
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            backend = OSVault().backend()
            service, account = "ai-coding-agent-artifacts", self.scope
            value = backend.get_password(service, account)
            if value is None:
                value = base64.b64encode(secrets.token_bytes(32)).decode()
                backend.set_password(service, account, value)
            return base64.b64decode(value, validate=True)
        except Exception:
            raise ValueError(
                "Artifact key could not be read or saved in the OS vault; no plaintext fallback was used."
            ) from None
        finally:
            os.close(descriptor)
            lock.unlink()

    def _mac(self, receipt):
        return hmac.new(self.auth_key, canonical(receipt), hashlib.sha256).hexdigest()

    def _paths(self, metadata):
        paths = metadata.get("paths", [])
        if (
            not isinstance(paths, list)
            or len(paths) > 256
            or any(not isinstance(path, str) or len(path) > 1024 for path in paths)
        ):
            raise ValueError("Too many or invalid artifact source paths.")
        for path in paths:
            self.workspace.path(path)

    def put(self, kind, payload, metadata=None):
        with self.lock:
            return self._put(kind, payload, metadata)

    def _put(self, kind, payload, metadata):
        if (
            kind not in {"check", "inspection", "task"}
            or not isinstance(payload, bytes)
            or len(payload) > MAX_ARTIFACT
        ):
            raise ValueError("Unsupported or oversized artifact.")
        receipt = {
            "version": 1,
            "id": uuid4().hex,
            "kind": kind,
            "scope": self.scope,
            "policy": policy_digest(self.workspace),
            "created": self.clock(),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "persistent": self.persistent,
            "metadata": json.loads(canonical(metadata or {})),
        }
        if len(canonical(receipt)) > 16384:
            raise ValueError("Artifact metadata is too large.")
        self._paths(receipt["metadata"])
        mac = self._mac(receipt)
        if self.persistent:
            self._persist(receipt, mac, payload)
        else:
            self.entries[receipt["id"]] = (receipt, mac, payload)
            while (
                len(self.entries) > 128
                or sum(len(value[2]) for value in self.entries.values()) > MAX_STORAGE
            ):
                self.entries.popitem(last=False)
        return {**json.loads(canonical(receipt)), "handle": receipt["id"] + "." + mac}

    def _persist(self, receipt, mac, payload):
        self._directory()
        lock = self.directory / ".write.lock"
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary = None
        try:
            self.prune()
            nonce = secrets.token_bytes(12)
            header = {key: value for key, value in receipt.items() if key != "metadata"}
            private = canonical(
                {
                    "payload": base64.b64encode(payload).decode(),
                    "metadata": receipt["metadata"],
                }
            )
            ciphertext = self.cipher.encrypt(nonce, private, canonical(header))
            encoded = canonical(
                {
                    "receipt": header,
                    "mac": mac,
                    "data": base64.b64encode(nonce + ciphertext).decode(),
                }
            )
            paths = list(self.directory.glob("*.json"))
            if len(paths) >= 1024:
                raise ValueError("Artifact entry quota reached.")
            used = sum(path.stat().st_size for path in paths if not path.is_symlink())
            if used + len(encoded) > MAX_STORAGE:
                raise ValueError("Artifact storage quota reached.")
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix=".artifact-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(self.directory / (receipt["id"] + ".json"))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            os.close(descriptor)
            lock.unlink()

    def get(self, handle):
        with self.lock:
            return self._get(handle)

    def _decode(self, value):
        header = value["receipt"]
        raw = base64.b64decode(value["data"], validate=True)
        private = json.loads(self.cipher.decrypt(raw[:12], raw[12:], canonical(header)))
        return (
            {**header, "metadata": private["metadata"]},
            value["mac"],
            base64.b64decode(private["payload"], validate=True),
        )

    def _get(self, handle):
        if not isinstance(handle, str) or not re.fullmatch(
            r"[0-9a-f]{32}\.[0-9a-f]{64}", handle
        ):
            raise ValueError("Invalid artifact handle.")
        identifier, signature = handle.split(".")
        try:
            if self.persistent:
                self._directory()
                path = self.directory / (identifier + ".json")
                if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT * 2:
                    raise ValueError("Invalid artifact file.")
                with path.open("rb") as stream:
                    data = json.loads(stream.read(MAX_ARTIFACT * 2 + 1))
                receipt, mac, payload = self._decode(data)
            else:
                receipt, mac, payload = self.entries[identifier]
            if (
                receipt["scope"] != self.scope
                or receipt["policy"] != policy_digest(self.workspace)
                or receipt["id"] != identifier
                or receipt["version"] != 1
                or not hmac.compare_digest(signature, mac)
                or not hmac.compare_digest(self._mac(receipt), mac)
                or not 0 <= self.clock() - receipt["created"] < TTL
            ):
                raise ValueError("Invalid, expired or unauthenticated artifact.")
            self._paths(receipt["metadata"])
            if (
                len(payload) != receipt["bytes"]
                or hashlib.sha256(payload).hexdigest() != receipt["sha256"]
            ):
                raise ValueError("Artifact digest mismatch.")
            return {**json.loads(canonical(receipt)), "handle": handle}, payload
        except Exception:
            raise ValueError(
                "Artifact unavailable, expired or unauthenticated; no operation was replayed."
            ) from None

    def read(self, handle, offset, length):
        if (
            type(offset) is not int
            or offset < 0
            or type(length) is not int
            or not 1 <= length <= 4096
        ):
            raise ValueError("Expected nonnegative byte offset and length 1–4096.")
        receipt, payload = self.get(handle)
        chunk = payload[offset : offset + length]
        return json.dumps(
            {
                "receipt": receipt,
                "offset": offset,
                "next_offset": offset + len(chunk),
                "eof": offset + len(chunk) >= len(payload),
                "base64": base64.b64encode(chunk).decode(),
            },
            ensure_ascii=False,
        )

    def prune(self):
        with self.lock:
            self._prune()

    def _prune(self):
        """Remove only authenticated expired entries; corrupt files are preserved."""
        if not self.persistent:
            return
        from cryptography.exceptions import InvalidTag

        for path in self.directory.glob("*.json"):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT * 2:
                    continue
                with path.open("rb") as stream:
                    value = json.loads(stream.read(MAX_ARTIFACT * 2 + 1))
                receipt = value["receipt"]
                if (
                    receipt["scope"] == self.scope
                    and self.clock() - receipt["created"] >= TTL
                ):
                    receipt, mac, payload = self._decode(value)
                    if (
                        hmac.compare_digest(self._mac(receipt), mac)
                        and len(payload) == receipt["bytes"]
                        and hashlib.sha256(payload).hexdigest() == receipt["sha256"]
                    ):
                        path.unlink()
            except (OSError, ValueError, KeyError, TypeError, InvalidTag):
                continue


def register_artifact_reader(registry, archive):
    from .tools import spec

    registry.register(
        spec(
            "archive_read",
            "Recover exact archived bytes without replaying tools. Returns base64, at most 4096 bytes.",
            {
                "handle": {"type": "string", "maxLength": 97},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 4096},
            },
            read_only=True,
        ),
        archive.read,
    )
