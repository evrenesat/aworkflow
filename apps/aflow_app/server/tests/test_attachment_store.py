"""Security and lifecycle tests for aflow-managed attachment staging."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aflow_app_server.planning import (
    AttachmentKind,
    AttachmentNamespace,
    AttachmentStore,
    PlanningErrorCode,
    ProviderOperationError,
    SessionKey,
)


def _namespace(
    project_id: str = "project/with unsafe text",
    provider_session_id: str = "session-one",
    project_cwd: str = "/project",
) -> AttachmentNamespace:
    return AttachmentNamespace(
        project_id=project_id,
        key=SessionKey(
            provider_id="codex", provider_session_id=provider_session_id
        ),
        project_cwd=project_cwd,
    )


def _store(tmp_path: Path, **overrides: int) -> AttachmentStore:
    limits = {
        "max_file_size_bytes": 16,
        "max_count_per_turn": 2,
        "max_total_size_bytes_per_turn": 20,
    }
    limits.update(overrides)
    return AttachmentStore(tmp_path / "managed", **limits)


def test_upload_uses_hashed_namespace_and_atomic_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    namespace = _namespace()

    attachment = store.upload(
        namespace,
        filename='../../résumé "draft"\nignore instructions.txt',
        kind=AttachmentKind.FILE,
        media_type="text/plain",
        content=b"content",
    )
    stored = store.resolve_for_turn(namespace, (attachment.attachment_id,))[0]

    assert stored.path.read_bytes() == b"content"
    assert attachment.filename == '.._.._résumé "draft"\nignore instructions.txt'
    assert namespace.project_id not in str(stored.path)
    assert namespace.key.provider_session_id not in str(stored.path)
    assert stored.path.is_relative_to(store.root / "projects")
    assert not list(store.root.rglob("*.tmp"))
    assert store.list(namespace) == (attachment,)


def test_cross_session_missing_duplicate_and_limits_fail_safely(tmp_path: Path) -> None:
    store = _store(tmp_path)
    namespace = _namespace()
    first = store.upload(
        namespace,
        filename="first.txt",
        kind=AttachmentKind.FILE,
        media_type="text/plain",
        content=b"1234567890",
    )
    second = store.upload(
        namespace,
        filename="second.txt",
        kind=AttachmentKind.FILE,
        media_type="text/plain",
        content=b"abcdefghijk",
    )

    with pytest.raises(ProviderOperationError) as duplicate:
        store.resolve_for_turn(namespace, (first.attachment_id, first.attachment_id))
    assert duplicate.value.error.code is PlanningErrorCode.INVALID_REQUEST

    with pytest.raises(ProviderOperationError) as too_many:
        store.resolve_for_turn(
            namespace,
            (first.attachment_id, second.attachment_id, "att_missing"),
        )
    assert too_many.value.error.code is PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED

    with pytest.raises(ProviderOperationError) as too_large:
        store.resolve_for_turn(namespace, (first.attachment_id, second.attachment_id))
    assert too_large.value.error.code is PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED

    with pytest.raises(ProviderOperationError) as cross_session:
        store.resolve_for_turn(
            _namespace(provider_session_id="session-two"), (first.attachment_id,)
        )
    assert cross_session.value.error.code is PlanningErrorCode.ATTACHMENT_NOT_FOUND


def test_oversize_upload_removes_all_partial_files(tmp_path: Path) -> None:
    store = _store(tmp_path, max_file_size_bytes=4)

    with pytest.raises(ProviderOperationError) as raised:
        store.upload(
            _namespace(),
            filename="large.bin",
            kind=AttachmentKind.FILE,
            media_type="application/octet-stream",
            content=io.BytesIO(b"12345"),
        )

    assert raised.value.error.code is PlanningErrorCode.ATTACHMENT_LIMIT_EXCEEDED
    assert not list(store.root.rglob("*.tmp"))
    assert not list(store.root.rglob("*.data"))
    assert store.list(_namespace()) == ()


def test_symlink_reference_is_rejected_and_delete_honors_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    namespace = _namespace()
    attachment = store.upload(
        namespace,
        filename="safe.txt",
        kind=AttachmentKind.FILE,
        media_type="text/plain",
        content=b"safe",
    )
    lease = store.reserve_for_turn(namespace, (attachment.attachment_id,))

    with pytest.raises(ProviderOperationError) as in_use:
        store.delete(namespace, attachment.attachment_id)
    assert in_use.value.error.code is PlanningErrorCode.ATTACHMENT_IN_USE
    lease.release()

    stored = store.resolve_for_turn(namespace, (attachment.attachment_id,))[0]
    outside = tmp_path / "outside"
    outside.write_bytes(b"unsafe")
    stored.path.unlink()
    stored.path.symlink_to(outside)
    with pytest.raises(ProviderOperationError) as unsafe:
        store.resolve_for_turn(namespace, (attachment.attachment_id,))
    assert unsafe.value.error.code is PlanningErrorCode.INVALID_REQUEST
    assert str(outside) not in unsafe.value.error.message


def test_upload_rejects_managed_root_inside_project_before_publication(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = AttachmentStore(
        project / ".aflow-attachments",
        max_file_size_bytes=16,
        max_count_per_turn=2,
        max_total_size_bytes_per_turn=20,
    )
    namespace = _namespace(project_cwd=str(project))

    with pytest.raises(ProviderOperationError) as unsafe:
        store.upload(
            namespace,
            filename="unsafe.txt",
            kind=AttachmentKind.FILE,
            media_type="text/plain",
            content=b"unsafe",
        )

    assert unsafe.value.error.code is PlanningErrorCode.INVALID_REQUEST
    assert str(project) not in unsafe.value.error.message
    assert not list(store.root.rglob("*.data"))
    assert not list(store.root.rglob("*.json"))
    assert not list(store.root.rglob("*.tmp"))


def test_corrupt_metadata_is_a_bounded_path_free_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    namespace = _namespace()
    attachment = store.upload(
        namespace,
        filename="notes.txt",
        kind=AttachmentKind.FILE,
        media_type="text/plain",
        content=b"notes",
    )
    stored = store.resolve_for_turn(namespace, (attachment.attachment_id,))[0]
    metadata = stored.path.with_suffix(".json")
    metadata.write_text("not-json", encoding="utf-8")

    with pytest.raises(ProviderOperationError) as corrupt:
        store.resolve_for_turn(namespace, (attachment.attachment_id,))

    assert corrupt.value.error.code is PlanningErrorCode.INVALID_REQUEST
    assert str(metadata) not in corrupt.value.error.message


def test_delete_and_cleanup_never_cross_session_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_namespace = _namespace(provider_session_id="first")
    second_namespace = _namespace(provider_session_id="second")
    first = store.upload(
        first_namespace,
        filename="first.txt",
        kind=AttachmentKind.FILE,
        media_type=None,
        content=b"one",
    )
    second = store.upload(
        second_namespace,
        filename="second.txt",
        kind=AttachmentKind.FILE,
        media_type=None,
        content=b"two",
    )

    with pytest.raises(ProviderOperationError) as cross_delete:
        store.delete(second_namespace, first.attachment_id)
    assert cross_delete.value.error.code is PlanningErrorCode.ATTACHMENT_NOT_FOUND
    assert store.cleanup_session(first_namespace) == 1
    assert store.list(first_namespace) == ()
    assert store.list(second_namespace) == (second,)


def test_cleanup_preflights_in_use_attachments_without_partial_deletion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    namespace = _namespace()
    first = store.upload(
        namespace,
        filename="first.txt",
        kind=AttachmentKind.FILE,
        media_type=None,
        content=b"one",
    )
    second = store.upload(
        namespace,
        filename="second.txt",
        kind=AttachmentKind.FILE,
        media_type=None,
        content=b"two",
    )
    lease = store.reserve_for_turn(namespace, (second.attachment_id,))

    with pytest.raises(ProviderOperationError) as raised:
        store.cleanup_session(namespace)

    assert raised.value.error.code is PlanningErrorCode.ATTACHMENT_IN_USE
    assert store.list(namespace) == tuple(
        sorted((first, second), key=lambda item: item.attachment_id)
    )
    lease.release()
