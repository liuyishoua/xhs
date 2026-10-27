"""Refresh only Leo's bound DM instructions; preserve routes and execution settings."""
import hashlib
import json
import os
import sys
import time
from fast_reply_agents import ROOT, instructions
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica
from channel_service.store import Store
from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount, User
from backend.app.models.direct_message import DmListener


def main():
    with SessionLocal() as db:
        account = db.get(PlatformAccount, 1)
        owner = db.get(User, account.user_id) if account else None
        listener = db.get(DmListener, 1)
        if not account or not owner or owner.username != 'leo' or account.nickname != 'leo' or account.platform != 'xhs' or account.sub_type != 'pc' or not listener:
            raise ValueError('Leo PC account identity could not be verified')
        binding = f'user:{account.user_id}:account:{account.external_user_id}'
        agent_id = (listener.agent_snapshot or {}).get('id')
        route_id = listener.route_id
    store = Store(ROOT / 'channel/.local')
    route = next(r for r in store.routes() if r['id'] == route_id)
    if route['agent_id'] != agent_id or route['account_ids'] != [binding] or route['execution_mode'] != 'frozen_reply':
        raise ValueError('Leo DM route does not match the bound identity')
    remote = Multica(ROOT / 'channel/.local/multica.json')
    before = remote.request('GET', '/api/agents/' + agent_id)
    if before['name'] != '小红书-leo · 私信':
        raise ValueError('Unexpected bound Agent')
    prompt = instructions('xhs-leo', binding)
    preserved = ('model', 'thinking_level', 'runtime_id', 'permission_mode', 'max_concurrent_tasks', 'skills', 'service_tier')
    folder = ROOT / 'xhs/.local/backups' / ('leo-dm-persona-' + str(time.time_ns()))
    folder.mkdir(mode=0o700, parents=True)
    def save(name, data):
        fd = os.open(folder / name, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    save('before.json', {k: before.get(k) for k in ('id', 'name', 'instructions', *preserved)})
    if before['instructions'] != prompt:
        remote.request('PUT', '/api/agents/' + agent_id, {'instructions': prompt})
    after = remote.request('GET', '/api/agents/' + agent_id)
    assert after['instructions'] == prompt
    assert all(before.get(k) == after.get(k) for k in preserved)
    report = {'agent_id': agent_id, 'profile': 'xhs-leo', 'profile_version': 3,
              'instruction_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
              'execution_settings_preserved': True, 'real_social_sends': 0}
    save('verified.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
