"""Back up and migrate only provably owned account bindings; never edit history or enable sending."""
from pathlib import Path
from datetime import datetime
import json
import os
import shutil
import sqlite3
from sqlalchemy import select
from backend.app.core.database import engine, init_db, SessionLocal
from backend.app.models import PlatformAccount, ModelConfig
from backend.app.models.direct_message import DmListener
from backend.app.services.model_routing import channel_client, available_route, account_keys
from backend.app.services.content_automation import PRIVATE


def main():
    catalog = channel_client().catalog()
    directory = PRIVATE / 'backups' / ('account-agent-migration-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True, mode=0o700)
    paths = [Path(engine.url.database)] + [p for p in PRIVATE.glob('*automation*.sqlite3') if 'before-' not in p.name]
    for source in paths:
        if not source.is_file():
            continue
        target = directory / source.name
        with sqlite3.connect(source) as origin, sqlite3.connect(target) as backup:
            origin.backup(backup)
        target.chmod(0o600)
    for name in ('content-automation.json', 'outdoor-automation.json'):
        source = PRIVATE / name
        if source.is_file():
            target = directory / name
            shutil.copyfile(source, target)
            target.chmod(0o600)
    init_db()
    log = []
    with SessionLocal() as db:
        accounts = {a.id: a for a in db.scalars(select(PlatformAccount).where(PlatformAccount.platform == 'xhs', PlatformAccount.sub_type == 'pc'))}
        def binding(account, purpose):
            matches = [r for r in catalog['routes'] if r.get('agent') and '*' not in r.get('account_ids', ['*'])
                       and account_keys(account).intersection(r['account_ids']) and available_route(r, purpose)]
            if len({r['agent']['id'] for r in matches}) != 1:
                return None
            route = max(matches, key=lambda r: len(r['categories']))
            return route, dict(id=route['agent']['id'], name=route['agent']['name'], route_id=route['id'], route_version=route.get('version'))
        for listener in db.scalars(select(DmListener)):
            account = accounts.get(listener.account_id)
            choice = binding(account, 'dm') if account else None
            if choice:
                route, snapshot = choice
                listener.route_id, listener.agent_snapshot = route['id'], snapshot
                log.append({'type':'dm','account_id':account.id,'action':'bound','agent':snapshot['name']})
        for config in db.scalars(select(ModelConfig).where(ModelConfig.provider == 'multica-channel')):
            original = next((r for r in catalog['routes'] if r['id'] == config.model_name), None)
            candidates = [a for a in accounts.values() if a.user_id == config.user_id and original and '*' not in original.get('account_ids',['*']) and account_keys(a).intersection(original['account_ids'])]
            if len(candidates) == 1:
                choice = binding(candidates[0], config.model_type)
                if choice:
                    route, snapshot = choice
                    config.model_name, config.name, config.base_url = route['id'], snapshot['name'], 'account:' + str(candidates[0].id)
                    log.append({'type':'model','purpose':config.model_type,'account_id':candidates[0].id,'action':'bound'})
                    continue
            config.is_default = False
            log.append({'type':'model','purpose':config.model_type,'action':'removed_ambiguous_default'})
        db.commit()
        for path in paths[1:]:
            if not path.is_file(): continue
            with sqlite3.connect(path) as ledger, ledger:
                for key, serialized in ledger.execute("SELECT key,value FROM state WHERE key LIKE 'comment_settings:%'").fetchall():
                    user_id, account_id, external = json.loads(key.removeprefix('comment_settings:'))
                    account = accounts.get(account_id)
                    if not account or account.user_id != user_id or account.external_user_id != external: continue
                    choice = binding(account, 'comment')
                    if not choice: continue
                    route, snapshot = choice
                    settings = json.loads(serialized)
                    settings.update(route_id=route['id'], agent_id=snapshot['id'], agent_snapshot=snapshot)
                    ledger.execute('UPDATE state SET value=? WHERE key=?', (json.dumps(settings,ensure_ascii=False),key))
                    log.append({'type':'comment','account_id':account_id,'action':'bound','agent':snapshot['name']})
        for name in ('content-automation.json','outdoor-automation.json'):
            path = PRIVATE / name
            if not path.is_file(): continue
            config = json.loads(path.read_text())
            account = accounts.get(config.get('pc_account_id'))
            if not account or account.user_id != config.get('user_id') or account.external_user_id != config.get('expected_external_user_id'): continue
            choice = binding(account, 'comment')
            if not choice: continue
            route, snapshot = choice
            config.update(route_id=route['id'],agent_id=snapshot['id'],agent_snapshot=snapshot)
            temporary = path.with_suffix('.agent-migration.tmp')
            temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n'); temporary.chmod(0o600)
            temporary.replace(path)
    report = directory / 'migration-log.json'
    report.write_text(json.dumps(log, ensure_ascii=False, indent=2)+'\n'); report.chmod(0o600)
    print(json.dumps(log,ensure_ascii=False))

if __name__ == '__main__':
    main()
