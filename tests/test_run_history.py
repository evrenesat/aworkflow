import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from aflow.control_plane import RunRepository, LaunchManifest, create_launch_manifest
from aflow.control_plane.persistence import ControlConflictError, PersistenceError
from aflow.control_plane.run_history import RunHistory, DeletedRunError
from aflow.control_plane.run_activity import preparation_owner


def record(root, name):
    directory = root / '.aflow' / 'runs' / name
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text('{"status":"running"}')
    (directory / 'evidence.txt').write_text('Recovery evidence stays intact')


def test_history_round_trip_replay_pagination_and_retained_evidence(tmp_path):
    ids = ['a', 'b', 'c', '20260809T172123Z-abc12345']
    for name in ids:
        record(tmp_path, name)
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    repository = RunRepository(tmp_path)
    history = RunHistory(repository)
    result = history.mutate('a', state='archived', expected_revision=0, idempotency_key='archive')
    assert history.mutate('a', state='archived', expected_revision=0, idempotency_key='archive') == result
    assert repository.list_history(history='archived').runs[0].run_id == 'a'
    pages, cursor = [], None
    while True:
        page = repository.list_history(limit=1, cursor=cursor)
        pages.extend(run.run_id for run in page.runs)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == sorted(set(ids) - {'a'})
    history = RunHistory(RunRepository(tmp_path))
    history.mutate('a', state='visible', expected_revision=1, idempotency_key='restore')
    deleted = history.mutate('a', state='deleted', expected_revision=2, idempotency_key='delete')
    assert history.mutate('a', state='deleted', expected_revision=2, idempotency_key='delete') == deleted
    with pytest.raises(DeletedRunError):
        history.mutate('a', state='archived', expected_revision=0, idempotency_key='archive')
    with pytest.raises(DeletedRunError):
        history.mutate('a', state='visible', expected_revision=3, idempotency_key='resurrect')
    with pytest.raises(DeletedRunError):
        history.project(repository.get_run_status('a'), external=True)
    assert len(repository.list_runs().runs) == 4
    assert len(repository.list_history(history='all').runs) == 3
    legacy = ids[-1]
    history.mutate(legacy, state='archived', expected_revision=0, idempotency_key='legacy')
    assert history.read(legacy)['state'] == 'archived'
    assert all(path.read_bytes() == data for path, data in before.items())


def test_history_conflicts_and_exact_identity(tmp_path):
    record(tmp_path, 'a')
    history = RunHistory(RunRepository(tmp_path))
    def mutate(state):
        try:
            return history.mutate('a', state=state, expected_revision=0, idempotency_key=state)
        except (ControlConflictError, DeletedRunError):
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(mutate, ['archived', 'deleted']))
    assert sum(result is not None for result in results) == 1
    for name in ['../a', '/a', 'missing']:
        with pytest.raises(PersistenceError):
            history.mutate(name, state='archived', expected_revision=0, idempotency_key='key')
    other = tmp_path / 'other'
    other.mkdir()
    record(other, 'a')
    assert RunHistory(RunRepository(other)).read('a')['state'] == 'visible'


def test_history_rejects_symlink_artifacts(tmp_path):
    record(tmp_path, 'a')
    history = RunHistory(RunRepository(tmp_path))
    path = history._path('a')
    path.parent.mkdir()
    target = tmp_path / 'untouched'
    target.write_text('keep')
    path.symlink_to(target)
    with pytest.raises(PersistenceError):
        history.read('a')
    assert target.read_text() == 'keep'


def test_preparation_requires_current_owner_and_get_never_writes(tmp_path):
    create_launch_manifest(tmp_path, LaunchManifest(run_id='a', project_root=str(tmp_path), plan_path='plans/a.md', workflow_name='cp', max_turns=5))
    path = tmp_path / '.aflow' / 'start-requests' / 'a.json'
    path.parent.mkdir()
    value = {'schema_version': 1, 'run_id': 'a', 'state': 'preparing'}
    repo = RunRepository(tmp_path)
    for owner, expected in [(None, 'unknown'), (preparation_owner(), 'active'), ({**preparation_owner(), 'birth': 'old-process'}, 'inactive')]:
        path.write_text(json.dumps({**value, 'preparation_owner': owner}))
        before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
        run = repo.get_run_status('a')
        assert run.activity == expected
        assert run.status == ('launch_started' if expected == 'active' else 'needs_attention')
        repo.list_history()
        assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_invalid_startup_question_is_attention_and_valid_question_remains_actionable(tmp_path):
    create_launch_manifest(tmp_path, LaunchManifest(run_id='a', project_root=str(tmp_path), plan_path='plans/a.md', workflow_name='cp', max_turns=5))
    path = tmp_path / '.aflow' / 'start-requests' / 'a.json'
    path.parent.mkdir()
    record = {'schema_version': 1, 'run_id': 'a', 'state': 'awaiting_startup_answer', 'question': {'kind': 'invalid-kind', 'message': 'Choose'}}
    path.write_text(json.dumps(record))
    repo = RunRepository(tmp_path)
    assert repo.get_run_status('a').status == 'needs_attention'
    from aflow.api.models import StartupQuestionKind
    record['question']['kind'] = StartupQuestionKind.PICK_STEP.value
    path.write_text(json.dumps(record))
    assert repo.get_run_status('a').status == 'awaiting_startup_answer'
    assert repo.get_run_status('a').activity == 'unknown'


@pytest.mark.parametrize('field,value', [('schema_version', 99), ('run_id', 'another'), ('state', 'unknown'), ('revision', True)])
def test_history_rejects_corrupt_schema_and_identity(tmp_path, field, value):
    record(tmp_path, 'a')
    history = RunHistory(RunRepository(tmp_path))
    history.mutate('a', state='archived', expected_revision=0, idempotency_key='archive')
    path = history._path('a')
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(PersistenceError):
        history.read('a')
