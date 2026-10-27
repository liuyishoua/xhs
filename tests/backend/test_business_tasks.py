"""Temporary ledgers and synthetic transports only; no platform messages."""
import json
import pytest
from backend.app.services.business_tasks import BusinessLedger

@pytest.fixture
def env(tmp_path):
    now=[1000.0]; store=BusinessLedger(tmp_path/'tasks.sqlite3',clock=lambda:now[0])
    authority={'revision':1,'enabled':True,'expires_at':1030,'project_id':'project',
       'grants':[{'account_id':1,'operations':['profile.read','message.read','draft.create'],'scope':{'conversation_ids':['peer-a']}}]}
    store.set_authority('owner','goal',authority)
    body={'request_id':'request','goal_id':'goal','project_id':'project','action_id':'action','account_id':1,
       'operation':'profile.read','input':{},'authorization_revision':1,'origin':'headquarters'}
    return store,now,authority,body

def test_durable_dedup_and_conflict(env):
    s,now,a,b=env; task=s.submit('owner',b)
    reopened=BusinessLedger(s.path,clock=lambda:now[0])
    assert reopened.submit('owner',b)['id']==task['id']
    with pytest.raises(ValueError,match='conflict'): reopened.submit('owner',{**b,'input':{'different':True}})
    assert reopened.get('owner',task['id'])['status']=='queued'
    with pytest.raises(KeyError): reopened.get('other',task['id'])

@pytest.mark.parametrize('changes',[{'account_id':2},{'authorization_revision':2},{'project_id':'wrong'},
    {'operation':'dm.initiate'},{'operation':'message.read','input':{'conversation_id':'peer-b'}}])
def test_exact_authority_denied(env,changes):
    s,_,_,b=env
    with pytest.raises(ValueError): s.submit('owner',{**b,**changes})

def test_revoke_before_claim(env):
    s,_,a,b=env; t=s.submit('owner',b)
    s.set_authority('owner','goal',{**a,'revision':2,'enabled':False})
    assert s.get('owner',t['id'])['state']=='cancelled'
    assert not s.tick(lambda *args: pytest.fail('executed'),lambda *args:None)

def test_expiry_before_claim(env):
    s,now,a,b=env; t=s.submit('owner',b); now[0]=1031
    assert s.tick(lambda *args:pytest.fail('executed'),lambda *args:None)
    assert s.get('owner',t['id'])['state']=='cancelled'

def test_account_revoked_after_submit(env):
    s,_,_,b=env;t=s.submit('owner',b)
    def unavailable(*args): raise ValueError('account changed')
    s.tick(lambda *args:pytest.fail('executed'),unavailable)
    assert s.get('owner',t['id'])['state']=='cancelled'

def test_model_draft_is_not_delivery_receipt(env):
    s,_,_,b=env;t=s.submit('owner',{**b,'operation':'draft.create','input':{'title':'test','body':'synthetic'}})
    s.tick(lambda *args:{'source':'workbench_draft','coverage':'local_draft_only','data':{'draft_id':1,'published':False}},lambda *args:None)
    result=s.get('owner',t['id']);assert result['state']=='succeeded'
    assert result['result']['data']['published'] is False
    assert result['result']['source']!='platform_receipt'

def test_interrupted_read_recovers_local_write_uncertain(env):
    s,_,_,b=env;read=s.submit('owner',b);write=s.submit('owner',{**b,'request_id':'write','operation':'draft.create'})
    with s.connect() as db: db.execute("UPDATE tasks SET state='running'")
    s.recover()
    assert s.get('owner',read['id'])['state']=='queued'
    assert s.get('owner',write['id'])['state']=='uncertain'

def test_timeout_write_never_retried(env):
    s,_,_,b=env;t=s.submit('owner',{**b,'operation':'draft.create'})
    calls=[]
    def fail(*args): calls.append(1); raise TimeoutError()
    s.tick(fail,lambda *args:None);s.tick(fail,lambda *args:None)
    assert calls==[1] and s.get('owner',t['id'])['state']=='uncertain'

def test_matching_future_inbound_and_cross_principal_events(env):
    s,_,_,b=env;t=s.submit('owner',{**b,'operation':'message.read','input':{'conversation_id':'peer-a'}})
    with s.connect() as db: row=db.execute('SELECT * FROM tasks WHERE id=?',(t['id'],)).fetchone()
    data={'source':'workbench_inbound','text':'synthetic'}
    s.inbound(row,'peer-a','msg',data);s.inbound(row,'peer-a','msg',data)
    events=s.events('owner')['items'];matches=[x for x in events if x['kind']=='message.inbound']
    assert len(matches)==1 and matches[0]['payload']['project_id']=='project'
    assert not s.events('other')['items']
    assert not s.events('owner',goal='other-goal')['items']
    cursor=s.events('owner')['cursor'];assert not s.events('owner',cursor)['items']

def test_same_revision_lease_refresh_only(env):
    s,_,a,b=env;s.set_authority('owner','goal',{**a,'expires_at':1040})
    with pytest.raises(ValueError,match='conflict'):s.set_authority('owner','goal',{**a,'project_id':'new'})

def test_stale_and_long_leases_rejected(env):
    s,_,a,b=env
    with pytest.raises(ValueError):s.set_authority('owner','goal',{**a,'expires_at':5000})
    s.set_authority('owner','goal',{**a,'revision':2})
    with pytest.raises(ValueError):s.set_authority('owner','goal',a)

def test_cancel_only_queued(env):
    s,_,_,b=env;t=s.submit('owner',b);assert s.cancel('owner',t['id'])['state']=='cancelled'
    t=s.submit('owner',{**b,'request_id':'other'})
    with s.connect() as db:db.execute("UPDATE tasks SET state='running' WHERE id=?",(t['id'],))
    assert s.cancel('owner',t['id'])['state']=='running'


def test_per_account_expiry_does_not_expire_sibling(env):
    s,now,a,b=env
    grants=[{**a['grants'][0],'expires_at':1001},{'account_id':2,'operations':['profile.read'],'scope':{},'expires_at':1025}]
    s.set_authority('owner','goal',{**a,'revision':2,'grants':grants})
    first=s.submit('owner',{**b,'authorization_revision':2})
    second=s.submit('owner',{**b,'authorization_revision':2,'request_id':'sibling','account_id':2})
    now[0]=1002
    calls=[]
    s.tick(lambda p,b:calls.append(b['account_id']) or {'source':'synthetic'},lambda *args:None)
    s.tick(lambda p,b:calls.append(b['account_id']) or {'source':'synthetic'},lambda *args:None)
    assert s.get('owner',first['id'])['state']=='cancelled'
    assert s.get('owner',second['id'])['state']=='succeeded' and calls==[2]
