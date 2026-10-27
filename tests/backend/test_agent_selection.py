"""Synthetic account Agent selection and immutable-history regression checks."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from backend.app.services.model_routing import agent_choices, resolve_agent


def route(id, agent, accounts, category='reply_draft'):
    return dict(id=id, version=2, name=id, agent={'id': agent, 'name': agent}, account_ids=accounts,
                categories=[category], surfaces=['*'], event_types=['*'], kinds=['text','mixed'])


def test_account_agent_choices_deduplicate_and_block_other_account():
    account = SimpleNamespace(id=2, user_id=1, external_user_id='peach')
    catalog = {'routes': [route('peach-dm','peach',['2']), route('peach-extra','peach',['user:1:account:peach']),
                          route('leo-dm','leo',['1']), route('comment','comment',['2'],'lifestyle_editor')]}
    assert {a['id'] for a in agent_choices(catalog, 'dm', account)} == {'peach','leo','comment'}
    with pytest.raises(HTTPException) as exc:
        resolve_agent(catalog, 'dm', account, 'leo')
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        resolve_agent(catalog, 'dm', account, 'peach', 'leo-dm')
    selected, snapshot = resolve_agent(catalog, 'dm', account, 'peach')
    selected['agent']['name'] = 'renamed'
    assert snapshot == {'id':'peach','name':'peach','route_id':'peach-dm','route_version':2}


def test_dm_default_prefers_its_owned_fast_route_without_cross_account_selection():
    from backend.app.services.model_routing import account_default
    account=SimpleNamespace(id=2,user_id=1,external_user_id='peach',nickname='小桃')
    fast={**route('fast-peach','fast',['user:1:account:peach']), 'execution_mode':'frozen_reply','enabled':True}
    other={**route('fast-leo','wrong',['user:1:account:leo']), 'execution_mode':'frozen_reply','enabled':True}
    probe={**route('isolated-probe','fast',['user:1:account:peach']), 'execution_mode':'frozen_reply','enabled':True,'priority':0}
    catalog={'routes':[other,probe,fast,route('old','old',['2'])], 'agents':[{'id':'old','name':'小红书-小桃'}]}
    selected,_=account_default(catalog,'dm',account)
    assert selected['id']=='fast-peach'
    # An explicit choice remains authoritative; optimization is not a reroute.
    selected,_=resolve_agent(catalog,'dm',account,route_id='old')
    assert selected['id']=='old'


def test_comment_api_agent_binding_and_cross_account_rejection(monitoring, monkeypatch):
    from backend.app.api import comment_monitoring as api
    from backend.app.core.security import create_access_token
    monkeypatch.setattr(api, 'channel_client', lambda: SimpleNamespace(catalog=lambda: {'connected':True,
        'routes':[route('peach','peach',['2'],'lifestyle_editor'),route('leo','leo',['4'],'lifestyle_editor')]}))
    client, ledger = monitoring
    headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    request = dict(enabled=True,auto_reply=False,control_mode='request_approval',agent_id='peach')
    result = client.put('/api/xhs/comments/listeners/2',json=request,headers=headers)
    assert result.status_code == 200
    assert result.json()['agent_id'] == 'peach'
    assert result.json()['agent_snapshot']['route_id'] == 'peach'
    request['agent_id'] = 'leo'
    assert client.put('/api/xhs/comments/listeners/2',json=request,headers=headers).status_code == 400
    assert {a['id'] for a in client.get('/api/xhs/comments/routes?account_id=2',headers=headers).json()['agents']} == {'peach','leo'}

from tests.backend.test_comment_monitoring import monitoring


def test_dm_history_retains_original_agent_and_rebinding_blocks_send(database):
    from backend.app.models.direct_message import DmListener
    from backend.app.services import dm_service as dm
    from tests.backend.test_direct_messages import accept, FakeChannel
    with database() as db:
        listener = db.get(DmListener,1)
        listener.agent_snapshot = {'id':'original','name':'original account Agent','route_id':'route-a'}
        db.commit()
        row = accept(db)
        dm.poll_channel(db,row,FakeChannel())
        listener.route_id = 'route-b'
        listener.agent_snapshot = {'id':'new','name':'new account Agent','route_id':'route-b'}
        db.commit()
        assert dm.message_view(row)['agent_snapshot']['id'] == 'original'
        assert dm.claim_send(db,row.id) is None

from tests.backend.test_direct_messages import database


def test_dynamic_catalog_lists_unbound_agent_and_binds_explicit_selection(monkeypatch):
    from backend.app.services import model_routing
    account = SimpleNamespace(id=2, user_id=1, external_user_id='peach', nickname='小桃')
    catalog = {'agents':[{'id':'fresh','name':'new Agent'}, {'id':'system','name':'system Agent'}], 'routes':[]}
    calls=[]
    def request(method,path,payload):
        calls.append((method,path,payload))
        return {'route':{'id':'created','version':1,'agent_id':'system'}, 'agent':{'id':'system','name':'system Agent'}}
    monkeypatch.setattr(model_routing,'channel_client',lambda:SimpleNamespace(request=request))
    assert {a['id'] for a in agent_choices(catalog,'image',account)} == {'fresh','system'}
    _, snapshot = resolve_agent(catalog,'image',account,agent_id='system')
    assert snapshot['id'] == 'system'
    assert calls[0][2] == {'account_id':'user:1:account:peach','purpose':'image','agent_id':'system'}
    with pytest.raises(HTTPException):
        resolve_agent(catalog,'image',account,agent_id='deleted')


def test_account_config_requires_selection_and_preserves_arbitrary_explicit_agent(database,monkeypatch):
    from backend.app.services import model_routing
    from backend.app.models import PlatformAccount,ModelConfig
    def bindings(account,purpose):
        own = str(account.id)
        return {'routes':[route('own-image','own',[own],'image_analysis'),route('explicit-image','other',[own],'image_analysis')],
                'agents':[{'id':'own','name':'小红书-'+account.nickname},{'id':'other','name':'Other assistant'}]}
    with database() as db:
        account = db.get(PlatformAccount,1); account.nickname='leo';db.commit()
        catalog=bindings(account,'image');catalog['routes'][0]['agent']['name']='小红书-leo'
        with pytest.raises(HTTPException) as exc:
            model_routing.ensure_account_config(db,account,'image',catalog)
        assert exc.value.status_code == 400
        assert '选择并保存' in exc.value.detail
        config=ModelConfig(user_id=account.user_id,model_type='image',provider=model_routing.PROVIDER,
                           base_url='account:1',model_name='explicit-image',name='Other assistant',
                           encrypted_api_key='',is_default=True)
        db.add(config);db.commit()
        assert model_routing.ensure_account_config(db,account,'image',catalog).model_name=='explicit-image'
        # Renaming the account and duplicate agent names do not affect an explicit binding.
        account.nickname='renamed'
        catalog['agents'].append({'id':'duplicate','name':'小红书-leo'})
        assert model_routing.ensure_account_config(db,account,'image',catalog).model_name=='explicit-image'
        config.model_name='removed-route';db.commit()
        with pytest.raises(HTTPException) as exc:
            model_routing.ensure_account_config(db,account,'image',catalog)
        assert '重新选择' in exc.value.detail


def test_brain_saves_arbitrary_agent_for_proactive_interactions(database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import model_routing as api
    from backend.app.services import model_routing as service
    from backend.app.core.database import get_db
    from backend.app.core.deps import get_current_user
    from backend.app.models import User, PlatformAccount, ModelConfig
    from sqlalchemy import select
    catalog = {'connected': True, 'agents': [{'id': 'writer', 'name': '任意写作助手'}], 'routes': []}
    calls = []

    def request(method, path, payload):
        calls.append(payload)
        category = 'lifestyle_editor' if payload['purpose'] == 'comment' else 'content_analysis'
        selected = route(payload['purpose'] + '-' + payload['account_id'], 'writer', [payload['account_id']], category)
        selected['agent']['name'] = '任意写作助手'
        catalog['routes'].append(selected)
        return {'route': selected, 'agent': selected['agent']}

    channel = SimpleNamespace(catalog=lambda: catalog, request=request)
    monkeypatch.setattr(api, 'channel_client', lambda: channel)
    monkeypatch.setattr(service, 'channel_client', lambda: channel)
    app = FastAPI()
    app.include_router(api.router)

    def db_session():
        with database() as db:
            yield db

    app.dependency_overrides[get_db] = db_session
    app.dependency_overrides[get_current_user] = lambda: User(id=1)
    with database() as db:
        db.add(PlatformAccount(id=3, user_id=1, platform='xhs', sub_type='pc', external_user_id='third', nickname='另一个账号'))
        db.commit()
    with TestClient(app) as client:
        before = client.get('/model-routing?account_id=1').json()
        assert before['agent_choices']['comment'][0]['id'] == 'writer'
        assert before['bindings']['comment'] is None
        saved = client.put('/model-routing/text', json={'account_id': 1, 'agent_id': 'writer'})
        assert saved.status_code == 200
        assert calls == [{'account_id': 'user:1:account:own', 'purpose': 'text', 'agent_id': 'writer'}]
        assert client.get('/model-routing?account_id=1').json()['bindings']['comment'] == 'comment-1'
        assert client.put('/model-routing/text', json={'account_id': 2, 'agent_id': 'writer'}).status_code == 404
        assert client.put('/model-routing/text', json={'account_id': 3, 'agent_id': 'writer'}).status_code == 200
        assert client.get('/model-routing?account_id=3').json()['bindings']['comment'] == 'comment-3'
        assert client.get('/model-routing?account_id=1').json()['bindings']['comment'] == 'comment-1'
    with database() as db:
        config = service.ensure_account_config(db, db.get(PlatformAccount, 1), 'comment', catalog)
        assert config.model_name == 'comment-1'
        assert config._agent_snapshot['id'] == 'writer'
        other = service.ensure_account_config(db, db.get(PlatformAccount, 3), 'comment', catalog)
        assert other.model_name == 'comment-3'
        assert other._agent_snapshot['id'] == 'writer'
        assert db.scalars(select(ModelConfig).where(ModelConfig.model_type == 'comment')).first() is None
