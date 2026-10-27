"""Independent publishing must preserve account and stop-control boundaries."""
import json
from types import SimpleNamespace

import pytest

from backend.app.services import content_automation as mod


def profile():
    return dict(user_id=1, pc_account_id=8, creator_account_id=9,
                expected_external_user_id='outdoor', autopilot_id='outdoor-pilot',
                publishing_only=True, enabled=True)


def test_publishing_ledgers_are_separate_from_comments_and_other_accounts(tmp_path):
    a = profile()
    publishing = mod.comment_ledger_path(a, tmp_path)
    comments = mod.comment_ledger_path({**a, 'publishing_only': False, 'comment_only': True}, tmp_path)
    other = mod.comment_ledger_path({**a, 'pc_account_id': 10}, tmp_path)
    rebound = mod.comment_ledger_path({**a, 'expected_external_user_id': 'replacement'}, tmp_path)
    assert len({publishing, comments, other, rebound, tmp_path / 'content-automation.sqlite3'}) == 5


def test_stop_control_reads_own_profile_and_rejects_account_rebinding(tmp_path):
    config = profile()
    target = tmp_path / 'profile.json'
    target.write_text(json.dumps(config))
    remote = SimpleNamespace(request=lambda *args: {'autopilot': {'status': 'active'}})
    ledger = mod.Ledger(tmp_path / 'ledger.sqlite3')
    worker = mod.Executor(config, ledger=ledger, remote=remote, channel=object(), config_file=target)
    worker.assert_enabled()
    target.write_text(json.dumps({**config, 'enabled': False}))
    with pytest.raises(mod.Paused):
        worker.assert_enabled()
    target.write_text(json.dumps({**config, 'creator_account_id': 22}))
    with pytest.raises(mod.Paused, match='发布账号配置已变化'):
        worker.assert_enabled()
    ledger.db.close()


def test_publishing_worker_does_not_poll_or_send_comments(tmp_path):
    ledger = mod.Ledger(tmp_path / 'ledger.sqlite3')
    ledger.add('foreign-comment', 'comment', {})
    remote = SimpleNamespace(request=lambda *args: {'autopilot': {'status': 'active'}})
    worker = mod.Executor(profile(), ledger=ledger, remote=remote, channel=object())
    worker.schedule = worker.ingest_runs = worker.verify_posts = lambda: None
    def forbidden(*args):
        pytest.fail('Dedicated publishing worker attempted comment I/O')
    worker.poll_comments = worker.verify_comments = worker.process_comment = forbidden
    worker.tick()
    assert ledger.entry('foreign-comment')['stage'] == 'received'
    assert ledger.state('execution') == 'active'
    ledger.db.close()
