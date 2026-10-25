import base64
import json
from pathlib import Path
import urllib.error

import pytest

from doctor.heal import Healer


class GH:
    def __init__(self, data):
        self.data = data

    def get(self, path, **params):
        assert path == '/repos/o/r/contents/batch-production.json' and params == {'ref': 'state'}
        if isinstance(self.data, Exception):
            raise self.data
        return {'content': base64.b64encode(json.dumps(self.data).encode()).decode()}


def healer(tmp_path, data):
    h = Healer.__new__(Healer)
    h.repo, h.target, h.gh = 'o/r', tmp_path, GH(data)
    (tmp_path/'strategy').mkdir()
    (tmp_path/'strategy/batch-2026-10-25.json').write_text('{}')
    return h


@pytest.mark.parametrize('job', [
    {'premise': 'e07', 'stage': 'held', 'run_id': 5},
    {'premise': 'e07', 'stage': 'building', 'run_id': 9, 'obsolete_runs': [5]},
    {'premise': 'e07', 'stage': 'upload_intent', 'run_id': 9, 'repair_history': [{'run_id': 5}]},
    {'premise': 'e07', 'stage': 'scheduled', 'run_id': 5},
])
def test_failed_producer_owned_by_batch_never_restarts_outside_its_coordinator(tmp_path, job):
    h = healer(tmp_path, {'enabled': True, 'jobs': [job]})
    assert 'owns this run' in h.batch_recovery_guard('5', 'run.yml')


def test_active_batch_reservation_also_protects_unlinked_producer(tmp_path):
    h = healer(tmp_path, {'enabled': True, 'jobs': [{'stage': 'building', 'run_id': 9}]})
    assert 'owns this producer' in h.batch_recovery_guard('5', 'shorts.yml')
    assert h.batch_recovery_guard('5', 'episode.yml') is None
    assert h.batch_recovery_guard('5', 'tests.yml') is None


def test_finished_batch_releases_unrelated_new_production(tmp_path):
    h = healer(tmp_path, {'enabled': True, 'jobs': [{'stage': 'scheduled', 'run_id': 9}]})
    assert h.batch_recovery_guard('5', 'run.yml') is None


@pytest.mark.parametrize('data', [urllib.error.URLError('offline'), {'jobs': None}, {}, {'jobs': []},
                                  {'jobs':[{'stage':'held','obsolete_runs':None}]}])
def test_unknown_recovery_state_preserves_existing_intents_and_holds(tmp_path, data):
    h = healer(tmp_path, data)
    assert 'unavailable' in h.batch_recovery_guard('5', 'run.yml')


def test_unrelated_channels_make_no_new_state_api_call(tmp_path):
    h = healer(tmp_path, RuntimeError('must not call API'))
    (tmp_path/'strategy/batch-2026-10-25.json').unlink()
    assert h.batch_recovery_guard('5', 'run.yml') is None


def test_heal_stops_before_log_classification_rerun_or_code_diagnosis(tmp_path):
    h = healer(tmp_path, {'enabled': True, 'jobs': [{'premise': 'e07', 'stage': 'held', 'run_id': 5}]})
    get = h.gh.get
    run = {'id':5,'html_url':'u','name':'run','path':'.github/workflows/run.yml','run_attempt':1,
           'conclusion':'failure','head_branch':'main','head_sha':'old'}
    h.gh.get = lambda path, **params: (run if path.endswith('/runs/5') else
                                     {'default_branch':'main'} if path == '/repos/o/r' else get(path, **params))
    h.lane, h.dry, h.events = {'workflows':{}}, False, []
    finished = []
    h.finish = lambda *args: finished.append(args)
    h.logs = lambda *args: pytest.fail('batch-owned production must stop before retry classification')
    h.heal('5')
    assert finished[0][1] == 'batch-owned' and finished[0][3] is None
