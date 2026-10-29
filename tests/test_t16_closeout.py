"""收尾验收：证据快照、审批恢复、文件版本与副作用防重。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

import app.api as api
import src.workflow as wf
from src.config import JOB_VERSION
from src.providers.mock_calendar import MockCalendar
from src.providers.mock_notifier import MockNotifier
from src.review import submit_review, VersionConflictError
from src.schemas import Assessment, Review, ReviewDecision, Booking, BookingStatus
from src.scheduler import create_round, close_round, schedule_round, run_external, retry_action
from src.task_store import get_task_store
from test_t8_review_extractor_consistency import _minimal_pdf

client = TestClient(api.app)


def analyze(tmp_path, batch='closeout', text='English business usage overseas project management'):
    repo = api._repo()
    repo.create_batch(batch, JOB_VERSION)
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(_minimal_pdf(text))
    result = wf.run_single(str(path), batch_id=batch)
    return path, result


def test_same_path_changed_content_preserves_both_source_versions(tmp_path):
    path, first = analyze(tmp_path, 'before', 'English overseas management')
    original = path.read_bytes()
    _, second = analyze(tmp_path, 'after', 'Domestic accounting work')
    assert first['document_id'] != second['document_id']
    assert client.get(f"/api/documents/{first['document_id']}/file").content == original
    assert client.get(f"/api/documents/{second['document_id']}/file").content == path.read_bytes()
    assert 'English' in client.get(f"/api/documents/{first['document_id']}/review").json()['pages'][0]['text']
    assert 'Domestic' in client.get(f"/api/documents/{second['document_id']}/review").json()['pages'][0]['text']


def test_new_batch_keeps_old_assessment_and_needs_new_approval(tmp_path):
    path, first = analyze(tmp_path, 'before')
    repo = api._repo()
    submit_review(first['assessment_id'], JOB_VERSION, 'approve', request_id='before_req')
    repo.create_batch('after', JOB_VERSION)
    second = wf.run_single(str(path), batch_id='after')
    assert first['assessment_id'] != second['assessment_id']
    assert repo.get_assessment(first['assessment_id'])
    assert repo.list_approved_candidates('before', JOB_VERSION)
    assert repo.list_approved_candidates('after', JOB_VERSION) == []
    assert repo.count_assessments_total() == 1
    assert repo.count_approved_distinct() == 0
    old = client.get(f"/api/documents/{first['document_id']}/review?assessment_id={first['assessment_id']}").json()
    assert old['is_current'] is False
    assert old['review']['consumed'] is True
    assert client.get(f"/api/documents/{first['document_id']}/review").json()['review'] is None


def test_stale_browser_cannot_approve_new_evidence(tmp_path):
    path, first = analyze(tmp_path, 'before')
    api._repo().create_batch('after', JOB_VERSION)
    wf.run_single(str(path), batch_id='after')
    result = client.post(f"/api/documents/{first['document_id']}/approve", json={
        'decision': 'approve', 'assessment_id': first['assessment_id'],
        'expected_version': JOB_VERSION, 'request_id': 'stale'})
    assert result.status_code == 409
    assert api._repo().get_reviews(first['assessment_id']) == []


def test_approval_request_replay_is_idempotent_and_bound_to_decision(tmp_path):
    _, result = analyze(tmp_path)
    url = f"/api/documents/{result['document_id']}/approve"
    body = {'assessment_id': result['assessment_id'], 'expected_version': JOB_VERSION,
            'request_id': 'browser_req', 'decision': 'approve'}
    first = client.post(url, json=body)
    second = client.post(url, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()['review_id'] == second.json()['review_id']
    assert first.json()['recovery_pending'] is False
    assert len(api._repo().get_reviews(result['assessment_id'])) == 1
    assert client.post(url, json={**body, 'decision': 'hold'}).status_code == 409


def test_saved_approval_can_be_resumed_after_api_failure(tmp_path, monkeypatch):
    _, result = analyze(tmp_path)
    import src.review as reviews
    resume = reviews._resume_assessment
    def fail(*args):
        raise RuntimeError('simulated process interruption')
    monkeypatch.setattr(reviews, '_resume_assessment', fail)
    body = {'assessment_id': result['assessment_id'], 'expected_version': JOB_VERSION, 'request_id': 'recover', 'decision': 'approve'}
    url = f"/api/documents/{result['document_id']}/approve"
    first = client.post(url, json=body)
    assert first.status_code == 200 and first.json()['recovery_pending'] is True
    assert api._repo().get_unconsumed_review(result['assessment_id'])
    assert client.get('/api/scheduling?batch_id=closeout').json()['pending_count'] == 1
    assert client.get('/api/batches/closeout').json()['rows'][0]['review_decision'] == 'pending'
    monkeypatch.setattr(reviews, '_resume_assessment', resume)
    second = client.post(url, json=body)
    assert second.json()['recovery_pending'] is False
    assert second.json()['review_id'] == first.json()['review_id']


def test_approval_recovery_in_fresh_python_process(tmp_path):
    _, result = analyze(tmp_path)
    repo = api._repo()
    repo.create_review(Review(id='saved_review', assessment_id=result['assessment_id'],
                             assessment_version=JOB_VERSION, decision=ReviewDecision.APPROVE,
                             request_id='fresh_process_req'))
    run = repo.list_runs(assessment_id=result['assessment_id'])[0]
    env = {**os.environ, 'RESUME_DB_PATH': str(repo.db_path),
           'RESUME_CHECKPOINT_PATH': str(wf.CHECKPOINT_DB_PATH),
           'MODEL_ENABLED': '0', 'AUTH_ENABLED': '0', 'PII_ENCRYPT_KEY': ''}
    command = "import sys,json; from src.workflow import resume_run; r=resume_run(sys.argv[1]); print(json.dumps({'decision':r['审批状态']}))"
    completed = subprocess.run([sys.executable, '-c', command, run['id']], env=env,
                               cwd=Path(__file__).resolve().parents[1], capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=40)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1])['decision'] == 'approve'
    assert repo.get_run(run['id'])['status'] == 'finished'
    assert repo.get_unconsumed_review(result['assessment_id']) is None


@pytest.mark.parametrize('failure', ['meeting', 'notify'])
def test_retry_keeps_booking_and_successful_actions(tmp_path, failure):
    _, result = analyze(tmp_path)
    repo = api._repo()
    submit_review(result['assessment_id'], JOB_VERSION, 'approve', request_id='scheduled')
    round_ = create_round('closeout')
    close_round(round_.id, JOB_VERSION)
    booking = schedule_round(round_.id)[0]
    class Calendar(MockCalendar):
        calls = 0
        def create_meeting(self, *args):
            self.calls += 1
            return super().create_meeting(*args)
    class Notifier(MockNotifier):
        calls = 0
        def send(self, *args):
            self.calls += 1
            return super().send(*args)
    calendar = Calendar(fail_times=int(failure == 'meeting'))
    notifier = Notifier(fail_times=int(failure == 'notify'))
    assert run_external(booking, calendar=calendar, notifier=notifier)['status'] == 'failed'
    if failure == 'meeting':
        assert notifier.calls == 0
    meeting_before = repo.get_action_by_booking(booking.id, 'meeting')['provider_id']
    assert run_external(booking, calendar=calendar, notifier=notifier)['status'] == 'succeeded'
    meeting_after = repo.get_action_by_booking(booking.id, 'meeting')['provider_id']
    if failure == 'notify':
        assert meeting_before == meeting_after
        assert calendar.calls == 1
    counts = (calendar.calls, notifier.calls)
    run_external(booking, calendar=calendar, notifier=notifier)
    retry_action(repo.get_action_by_booking(booking.id, 'notify')['id'], calendar=calendar, notifier=notifier)
    assert counts == (calendar.calls, notifier.calls)
    assert [b.id for b in schedule_round(round_.id)] == [booking.id]
    assert len(repo.get_bookings(round_.id)) == 1


def test_unavailable_slots_stay_waitlisted(tmp_path):
    _, result = analyze(tmp_path)
    submit_review(result['assessment_id'], JOB_VERSION, 'approve', request_id='unavailable')
    round_ = create_round('closeout')
    close_round(round_.id, JOB_VERSION)
    assert schedule_round(round_.id, availability={result['document_id']: []})[0].status.value == 'waitlist'


def test_interrupted_scheduling_reuses_confirmed_and_pending_bookings(tmp_path):
    _, first = analyze(tmp_path, text='English overseas management')
    repo = api._repo()
    second_file = tmp_path / 'second.pdf'
    second_file.write_bytes(_minimal_pdf('Different English overseas management'))
    second = wf.run_single(str(second_file), batch_id='closeout')
    for i, result in enumerate((first, second)):
        submit_review(result['assessment_id'], JOB_VERSION, 'approve', request_id=f'booking_{i}')
    round_ = create_round('closeout')
    close_round(round_.id, JOB_VERSION)
    for i, result in enumerate((first, second)):
        repo.create_booking(Booking(id=f'existing_{i}', round_id=round_.id,
                                    candidate_id=result['document_id'], slot_id=None,
                                    status=BookingStatus.PENDING, idempotency_key=f'existing_key_{i}'))
    assert repo.claim_slot('existing_0', repo.get_slots(round_.id)[0]['id'])
    bookings = schedule_round(round_.id)
    assert {b.id for b in bookings} == {'existing_0', 'existing_1'}
    assert all(b.status == BookingStatus.CONFIRMED for b in bookings)
    assert len({b.slot_id for b in bookings}) == 2


def test_startup_marks_orphaned_tasks_interrupted():
    store = get_task_store()
    store.create({'task_id': 'orphan', 'batch_id': 'lost', 'status': 'running', 'total': 4,
                  'done': 2, 'rows': [], 'extractor': 'mock', 'error': ''})
    with TestClient(api.app) as started:
        response = started.get('/api/batches/tasks/orphan').json()
    assert response['status'] == 'interrupted'
    assert response['done'] == 2
    assert response['error']


def test_invalid_parse_preserves_manual_review_path(tmp_path):
    repo = api._repo()
    repo.create_batch('bad', JOB_VERSION)
    path = tmp_path / 'bad.pdf'
    path.write_bytes(b'%PDF-invalid')
    result = wf.run_single(str(path), batch_id='bad')
    review = client.get(f"/api/documents/{result['document_id']}/review").json()
    assert review['assessment']['recommendation'] == 'needs_review'
    assert review['warnings']
    assert client.post(f"/api/documents/{result['document_id']}/approve", json={'decision': 'needs_review', 'assessment_id': result['assessment_id'], 'expected_version': JOB_VERSION, 'request_id': 'manual_bad'}).status_code == 200


def test_upload_same_name_different_bytes_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'RESUME_DIR', tmp_path)
    original = _minimal_pdf('English')
    changed = _minimal_pdf('Different content')
    first = client.post('/api/upload', files={'file': ('same.pdf', original, 'application/pdf')}).json()
    second = client.post('/api/upload', files={'file': ('same.pdf', changed, 'application/pdf')}).json()
    assert first['file_name'] != second['file_name']
    assert (tmp_path / first['file_name']).read_bytes() == original
    assert (tmp_path / second['file_name']).read_bytes() == changed


def test_assessment_snapshot_cannot_be_overwritten(tmp_path):
    _, result = analyze(tmp_path)
    repo = api._repo()
    before = repo.get_assessment(result['assessment_id'])
    repo.save_assessment(Assessment(id=result['assessment_id'], document_id=result['document_id'],
                                   job_version=JOB_VERSION, known_score=99))
    assert repo.get_assessment(result['assessment_id']) == before


def test_api_requires_reviewed_snapshot_identity(tmp_path):
    _, result = analyze(tmp_path)
    response = client.post(f"/api/documents/{result['document_id']}/approve", json={'decision': 'approve'})
    assert response.status_code == 428
    assert api._repo().get_reviews(result['assessment_id']) == []


def test_closed_batch_rejects_new_decisions(tmp_path):
    _, result = analyze(tmp_path)
    api._repo().freeze_batch('closeout')
    with pytest.raises(VersionConflictError, match='结束审批'):
        submit_review(result['assessment_id'], JOB_VERSION, 'approve', request_id='too_late')


def test_unexpected_worker_failure_is_visible(monkeypatch):
    store = get_task_store()
    task = {'task_id': 'failed_worker', 'batch_id': 'worker', 'status': 'running'}
    store.create(task)
    def fail(*args):
        raise RuntimeError('synthetic worker failure')
    monkeypatch.setattr(api, '_process_batch', fail)
    api._batch_worker(task, [])
    assert store.get('failed_worker')['status'] == 'failed'
    assert store.get('failed_worker')['error'] == 'synthetic worker failure'


def test_scheduling_without_approval_does_not_close_batch(tmp_path):
    analyze(tmp_path)
    response = client.post('/api/scheduling/run?batch_id=closeout')
    assert response.status_code == 409
    assert api._repo().get_batch('closeout')['status'] == 'open'
