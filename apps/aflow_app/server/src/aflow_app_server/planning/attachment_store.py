"""Safe aflow-managed storage for provider-neutral planning attachments."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .models import (
    Attachment,
    AttachmentKind,
    PlanningError,
    PlanningErrorCode,
    SessionKey,
)
from .provider import ProviderOperationError


_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class AttachmentNamespace:
    """Exact app-owned namespace for one project's provider session."""

    project_id: str
    key: SessionKey
    project_cwd: str

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("project_id must not be empty")
        if not self.project_cwd:
            raise ValueError("project_cwd must not be empty")


@dataclass(frozen=True)
class StoredAttachment:
    attachment: Attachment
    path: Path


class AttachmentLease:
    """In-flight references that prevent attachment deletion."""

    def __init__(
        self,
        store: AttachmentStore,
        namespace: AttachmentNamespace,
        attachments: tuple[StoredAttachment, ...],
    ) -> None:
        self._store = store
        self._namespace = namespace
        self.attachments = attachments
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._store._release(self._namespace, self.attachments)


class AttachmentStore:
    """Persist uploads outside repositories using hashed namespace components."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_size_bytes: int,
        max_count_per_turn: int,
        max_total_size_bytes_per_turn: int,
    ) -> None:
        if min(
            max_file_size_bytes,
            max_count_per_turn,
            max_total_size_bytes_per_turn,
        ) <= 0:
            raise ValueError("attachment limits must be greater than zero")
        self._root = root.expanduser().absolute()
        self._max_file_size = max_file_size_bytes
        self._max_count = max_count_per_turn
        self._max_total_size = max_total_size_bytes_per_turn
        self._lock = threading.RLock()
        self._in_use: dict[tuple[str, str, str, str], int] = {}
        self._ensure_directory(self._root)
        self._root = self._root.resolve(strict=True)

    @property
    def root(self) -> Path:
        return self._root

    def upload(
        self,
        namespace: AttachmentNamespace,
        *,
        filename: str,
        kind: AttachmentKind,
        media_type: str | None,
        content: bytes | BinaryIO,
    ) -> Attachment:
        """Atomically publish uploaded bytes and their metadata."""
        with self._lock:
            try:
                self._assert_outside_project(self._root, namespace)
                incoming = self._incoming_directory(namespace, create=True)
            except ProviderOperationError:
                raise
            except Exception as exc:
                raise self._storage_error(namespace) from exc
            attachment_id = f"att_{uuid.uuid4().hex}"
            data_path = incoming / f"{attachment_id}.data"
            metadata_path = incoming / f"{attachment_id}.json"
            temporary_path = incoming / f".upload-{uuid.uuid4().hex}.tmp"
            stream = io.BytesIO(content) if isinstance(content, bytes) else content
            size = 0
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(temporary_path, flags, 0o600)
                with os.fdopen(descriptor, "wb") as destination:
                    while chunk := stream.read(_CHUNK_SIZE):
                        if not isinstance(chunk, bytes):
                            raise TypeError("attachment stream must return bytes")
                        size += len(chunk)
                        if size > self._max_file_size:
                            raise self._error(
                                namespace,
                                PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED,
                                "Attachment exceeds the configured per-file size limit.",
                            )
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                self._assert_regular_contained(temporary_path, incoming)
                os.replace(temporary_path, data_path)
                attachment = Attachment(
                    attachment_id=attachment_id,
                    filename=self._display_filename(filename),
                    kind=kind,
                    media_type=media_type,
                    size_bytes=size,
                    created_at=datetime.now(timezone.utc),
                )
                self._atomic_json(
                    metadata_path,
                    {
                        "namespace": self._namespace_payload(namespace),
                        "attachment": attachment.model_dump(mode="json"),
                        "data_file": data_path.name,
                    },
                )
                return attachment
            except ProviderOperationError:
                temporary_path.unlink(missing_ok=True)
                if not metadata_path.exists():
                    data_path.unlink(missing_ok=True)
                raise
            except Exception as exc:
                temporary_path.unlink(missing_ok=True)
                if not metadata_path.exists():
                    data_path.unlink(missing_ok=True)
                raise self._storage_error(namespace) from exc

    def list(self, namespace: AttachmentNamespace) -> tuple[Attachment, ...]:
        with self._lock:
            try:
                incoming = self._incoming_directory(namespace, create=False)
                if incoming is None:
                    return ()
                attachments = [
                    self._read_stored(namespace, path.stem, incoming).attachment
                    for path in incoming.glob("att_*.json")
                ]
                return tuple(sorted(attachments, key=lambda item: item.attachment_id))
            except ProviderOperationError:
                raise
            except Exception as exc:
                raise self._storage_error(namespace) from exc

    def resolve_for_turn(
        self,
        namespace: AttachmentNamespace,
        attachment_ids: tuple[str, ...],
    ) -> tuple[StoredAttachment, ...]:
        with self._lock:
            try:
                if len(set(attachment_ids)) != len(attachment_ids):
                    raise self._error(
                        namespace,
                        PlanningErrorCode.INVALID_REQUEST,
                        "Duplicate attachment ids are not allowed in one turn.",
                    )
                if len(attachment_ids) > self._max_count:
                    raise self._error(
                        namespace,
                        PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED,
                        "Turn exceeds the configured attachment count limit.",
                    )
                incoming = self._incoming_directory(namespace, create=False)
                if incoming is None and attachment_ids:
                    raise self._not_found(namespace)
                stored = tuple(
                    self._read_stored(namespace, attachment_id, incoming)
                    for attachment_id in attachment_ids
                    if incoming is not None
                )
                if sum(item.attachment.size_bytes for item in stored) > self._max_total_size:
                    raise self._error(
                        namespace,
                        PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED,
                        "Turn exceeds the configured total attachment size limit.",
                    )
                return stored
            except ProviderOperationError:
                raise
            except Exception as exc:
                raise self._storage_error(namespace) from exc

    def reserve_for_turn(
        self,
        namespace: AttachmentNamespace,
        attachment_ids: tuple[str, ...],
    ) -> AttachmentLease:
        with self._lock:
            attachments = self.resolve_for_turn(namespace, attachment_ids)
            for stored in attachments:
                correlation = self._correlation(namespace, stored.attachment.attachment_id)
                self._in_use[correlation] = self._in_use.get(correlation, 0) + 1
            return AttachmentLease(self, namespace, attachments)

    def delete(self, namespace: AttachmentNamespace, attachment_id: str) -> None:
        """Remove one attachment after atomically hiding its metadata and bytes."""
        with self._lock:
            try:
                incoming = self._incoming_directory(namespace, create=False)
                if incoming is None:
                    raise self._not_found(namespace)
                stored = self._read_stored(namespace, attachment_id, incoming)
                correlation = self._correlation(namespace, attachment_id)
                if self._in_use.get(correlation, 0):
                    raise self._error(
                        namespace,
                        PlanningErrorCode.ATTACHMENT_IN_USE,
                        "Attachment is referenced by an in-flight turn.",
                    )
                metadata_path = incoming / f"{attachment_id}.json"
                token = uuid.uuid4().hex
                data_tombstone = incoming / f".delete-{token}.data"
                metadata_tombstone = incoming / f".delete-{token}.json"
                os.replace(stored.path, data_tombstone)
                try:
                    os.replace(metadata_path, metadata_tombstone)
                except Exception:
                    os.replace(data_tombstone, stored.path)
                    raise
                data_tombstone.unlink()
                metadata_tombstone.unlink()
            except ProviderOperationError:
                raise
            except Exception as exc:
                raise self._storage_error(namespace) from exc

    def cleanup_session(self, namespace: AttachmentNamespace) -> int:
        """Delete only validated, unused attachments in one exact namespace."""
        with self._lock:
            attachments = self.list(namespace)
            if any(
                self._in_use.get(
                    self._correlation(namespace, attachment.attachment_id), 0
                )
                for attachment in attachments
            ):
                raise self._error(
                    namespace,
                    PlanningErrorCode.ATTACHMENT_IN_USE,
                    "Session attachments are referenced by an in-flight turn.",
                )
            for attachment in attachments:
                self.delete(namespace, attachment.attachment_id)
            return len(attachments)

    def _release(
        self,
        namespace: AttachmentNamespace,
        attachments: tuple[StoredAttachment, ...],
    ) -> None:
        with self._lock:
            for stored in attachments:
                correlation = self._correlation(namespace, stored.attachment.attachment_id)
                remaining = self._in_use.get(correlation, 0) - 1
                if remaining > 0:
                    self._in_use[correlation] = remaining
                else:
                    self._in_use.pop(correlation, None)

    def _incoming_directory(
        self, namespace: AttachmentNamespace, *, create: bool
    ) -> Path | None:
        self._assert_outside_project(self._root, namespace)
        components = (
            "projects",
            self._key("project", namespace.project_id),
            self._key("provider", namespace.key.provider_id),
            self._key("session", namespace.key.provider_session_id),
        )
        current = self._root
        for component in components:
            candidate = current / component
            if candidate.is_symlink():
                raise ValueError("attachment namespace contains a symlink")
            if not candidate.exists() and not create:
                return None
            self._ensure_directory(candidate)
            self._assert_contained(candidate, self._root)
            current = candidate
        namespace_path = current / "namespace.json"
        payload = self._namespace_payload(namespace)
        if namespace_path.is_symlink():
            raise ValueError("attachment namespace metadata is a symlink")
        if namespace_path.exists():
            self._assert_regular_contained(namespace_path, current)
            if self._read_json(namespace_path) != payload:
                raise self._error(
                    namespace,
                    PlanningErrorCode.CONFLICT,
                    "Attachment namespace metadata does not match the requested session.",
                )
        elif create:
            self._atomic_json(namespace_path, payload)
        else:
            return None
        incoming = current / "incoming"
        if incoming.is_symlink():
            raise ValueError("attachment incoming directory is a symlink")
        if not incoming.exists() and not create:
            return None
        self._ensure_directory(incoming)
        self._assert_contained(incoming, self._root)
        self._assert_outside_project(incoming, namespace)
        return incoming

    def _read_stored(
        self, namespace: AttachmentNamespace, attachment_id: str, incoming: Path
    ) -> StoredAttachment:
        if not attachment_id.startswith("att_") or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in attachment_id
        ):
            raise self._not_found(namespace)
        metadata_path = incoming / f"{attachment_id}.json"
        if metadata_path.is_symlink():
            raise ValueError("attachment metadata is a symlink")
        if not metadata_path.exists():
            raise self._not_found(namespace)
        self._assert_regular_contained(metadata_path, incoming)
        payload = self._read_json(metadata_path)
        if payload.get("namespace") != self._namespace_payload(namespace):
            raise self._not_found(namespace)
        try:
            attachment = Attachment.model_validate(payload["attachment"])
            data_file = payload["data_file"]
        except Exception as exc:
            raise self._error(
                namespace,
                PlanningErrorCode.INVALID_REQUEST,
                "Attachment metadata is invalid.",
            ) from exc
        if attachment.attachment_id != attachment_id or data_file != f"{attachment_id}.data":
            raise self._error(
                namespace,
                PlanningErrorCode.INVALID_REQUEST,
                "Attachment metadata is invalid.",
            )
        data_path = incoming / data_file
        if data_path.is_symlink():
            raise ValueError("attachment data is a symlink")
        if not data_path.exists():
            raise self._not_found(namespace)
        self._assert_regular_contained(data_path, incoming)
        self._assert_outside_project(data_path, namespace)
        if data_path.stat().st_size != attachment.size_bytes:
            raise self._error(
                namespace,
                PlanningErrorCode.INVALID_REQUEST,
                "Attachment contents do not match their metadata.",
            )
        return StoredAttachment(attachment=attachment, path=data_path)

    def _atomic_json(self, destination: Path, payload: object) -> None:
        temporary = destination.parent / f".metadata-{uuid.uuid4().hex}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("attachment metadata is unreadable") from exc
        if not isinstance(value, dict):
            raise ValueError("attachment metadata must be an object")
        return value

    @staticmethod
    def _display_filename(filename: str) -> str:
        normalized = unicodedata.normalize("NFC", filename).replace("\x00", "")
        normalized = normalized.replace("/", "_").replace("\\", "_")
        normalized = normalized.strip()
        return normalized or "attachment"

    @staticmethod
    def _key(kind: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{kind}-{digest}"

    @staticmethod
    def _namespace_payload(namespace: AttachmentNamespace) -> dict[str, str]:
        return {
            "project_id": namespace.project_id,
            "provider_id": namespace.key.provider_id,
            "provider_session_id": namespace.key.provider_session_id,
        }

    @staticmethod
    def _correlation(
        namespace: AttachmentNamespace, attachment_id: str
    ) -> tuple[str, str, str, str]:
        return (
            namespace.project_id,
            namespace.key.provider_id,
            namespace.key.provider_session_id,
            attachment_id,
        )

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"attachment storage component is not a directory: {path}")

    @staticmethod
    def _assert_contained(path: Path, parent: Path) -> None:
        try:
            path.resolve(strict=True).relative_to(parent.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ValueError("attachment path escapes its managed namespace") from exc

    @classmethod
    def _assert_regular_contained(cls, path: Path, parent: Path) -> None:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("attachment path is not a regular file")
        cls._assert_contained(path, parent)

    def _not_found(self, namespace: AttachmentNamespace) -> ProviderOperationError:
        return self._error(
            namespace,
            PlanningErrorCode.ATTACHMENT_NOT_FOUND,
            "Attachment was not found in this planning session.",
        )

    def _storage_error(self, namespace: AttachmentNamespace) -> ProviderOperationError:
        return self._error(
            namespace,
            PlanningErrorCode.INVALID_REQUEST,
            "Attachment storage is unsafe or invalid.",
        )

    @staticmethod
    def _assert_outside_project(path: Path, namespace: AttachmentNamespace) -> None:
        project_root = Path(namespace.project_cwd).expanduser().resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(project_root)
        except ValueError:
            return
        raise ValueError("attachment storage overlaps the authorized project")

    @staticmethod
    def _error(
        namespace: AttachmentNamespace,
        code: PlanningErrorCode,
        message: str,
    ) -> ProviderOperationError:
        return ProviderOperationError(
            PlanningError(
                code=code,
                message=message,
                provider_id=namespace.key.provider_id,
                retryable=False,
            )
        )
