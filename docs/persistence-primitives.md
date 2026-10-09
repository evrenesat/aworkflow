# Persistence primitives and caller contracts

`aflow/file_io.py` provides four small explicit file operations shared by the
audited persistence callers. Each operation has one guarantee set and an
explicit mode where it creates a file; callers keep their own validation,
locks, schema checks, and transaction ordering, and choose the durability
order by composing the operations.

| Operation | Guarantee |
| --- | --- |
| `create_exclusive_file(path, data, *, mode)` | Writes `data` to an owned temporary file in the target directory, fsyncs the file, then publishes with `os.link`. The final name appears atomically and only when it does not already exist; an existing path raises `FileExistsError` with its bytes unchanged. The temporary file is removed on every failure path. |
| `atomic_replace_file(path, data, *, mode)` | Same owned temporary file and file fsync, then publishes with `os.replace`. No partial final write is ever exposed; the published file carries `mode` (subject to umask). The temporary file is removed on every failure path. |
| `fsync_directory(path)` | Opens the directory read-only, fsyncs it, and propagates every I/O failure (open, fsync, close). Callers whose established contract tolerates directory-sync failures catch `OSError` at the call site; runlog instead uses the narrower `_sync_runlog_directory` domain adapter, which tolerates only the directory open and propagates fsync/close failures. |
| `read_bounded_regular_file(path, max_bytes)` | Reads one non-symlink regular file of at most `max_bytes` bytes; the size is checked before and during the read. Symlinks, non-regular files, and oversize content raise `ValueError`. |

A single-file helper never makes a two-file save atomic; the config pair
transaction (maintainability member 02) remains its own authority.

## Caller contract table (audit)

Audited before choosing helper calls; differences are real, so each caller
keeps its domain adapter:

| Caller (helper) | Publish | Overwrite | Mode | Directory fsync | Distinct contract kept in domain |
| --- | --- | --- | --- | --- | --- |
| `runlog._write_atomic_json` / `_write_atomic_bytes` → `atomic_replace_file` | `os.replace` | yes | umask default (`0o666 & ~umask`) | best effort (open failure tolerated via `_sync_runlog_directory`; fsync/close failures propagate) | JSON formatting; parent `mkdir`; run-state schema validation stays in `RunMetadataWriter` |
| `runlog.write_repartition_artifact` → `create_exclusive_file` | `os.link` via exclusive temp | never (`FileExistsError`) | umask default | strict (failures propagate) | Immutable repartition attempt artifact; pre-existing-path rejection message |
| `runlog.store_evidence_artifact` → `atomic_replace_file` | `os.replace` | yes (digest-addressed idempotent) | umask default | none | Digest/byte-size verification, containment and symlink fail-closed checks |
| `publication._write_atomic_receipt` → `atomic_replace_file` | `os.replace` | yes | `0o600` | best effort (open failure tolerated via `_sync_receipt_directory`; fsync/close failures propagate) | Receipt JSON shape; publication receipt authority |
| `project_admission._save_locked` → domain-owned temporary writer + `fsync_directory` | `os.replace` | yes (under lock) | exact `0o600` via `os.fchmod` on the owned temporary descriptor before file fsync | strict (failures map to `ProjectAdmissionSafetyError`) | Admission lock ownership; symlink rejection; state payload cap; exact private mode under any umask |
| `plan_backups._write_atomic_json` | `os.replace` from `O_EXCL\|O_NOFOLLOW` temp | yes | `0o600` explicit | best effort (open and fsync tolerated) | Backup provenance; `pid` in temp name |
| `control_plane.persistence._write_exclusive_json` | `os.link` | never (`FileExistsError`) | `0o600` (mkstemp) | best effort | Immutable launch manifest; request-digest idempotency |
| `control_plane.persistence._write_atomic_bytes` | `os.replace` | yes | `0o600` (mkstemp) | best effort | Control-plane revision handling |
| `execution_resources._write_journal` | `os.replace` | yes (under lock) | `0o600` (mkstemp) | none | Journal size cap, revision/claim ordering, lock ownership |

Read-side notes: `project_admission` opens plan files with `O_NOFOLLOW`
bounded reads; `plan_backups` and `execution_resources` stream files in
1 MiB chunks with size caps. `read_bounded_regular_file` is the shared
primitive for these bounded regular-file reads when callers migrate.

## Migration status

- Checkpoint 1: `runlog` matching paths migrate to the primitives; its
  validation and error mapping stay at the runlog boundary.
- Checkpoint 2: `publication._write_atomic_receipt` migrates to
  `atomic_replace_file` plus its best-effort receipt directory-sync contract
  (fsync/close failures propagate). `project_admission._save_locked` keeps a
  small domain-owned temporary writer: the shared byte primitive applies its
  mode at temporary creation, so it is subject to the process umask and is
  not an equivalent admission writer that must publish an exact `0o600`
  descriptor before file fsync and rename. Admission therefore retains
  `tempfile.mkstemp` + `os.fchmod` and reuses only the strict
  `fsync_directory` primitive for the post-publication directory sync. Receipt
  and claim ownership stay with their domains.
- Checkpoint 3 migrates the remaining callers one at a time, preserving the
  contracts in the table above.

Effort: `AFLOW-MAINT-20261006` (member 14).
