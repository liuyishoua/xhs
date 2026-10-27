"""Owner-scoped business task ledger. Never dispatches arbitrary platform writes."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

SUPPORTED = {'profile.read', 'content.read', 'public.search', 'conversation.read', 'message.read', 'draft.create', 'dm.reply', 'content.publish', 'comment.reply', 'content.analyze', 'material.read'}
UNAVAILABLE = {'dm.initiate': '没有受支持的主动私信入口', 'video.publish': '视频发布未实现'}

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

class BusinessLedger:
    def __init__(self, path, clock=time.time):
        self.path = Path(path); self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS authority(principal TEXT, goal TEXT, revision INTEGER, body TEXT, PRIMARY KEY(principal,goal));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, principal TEXT, request_id TEXT, fingerprint TEXT, body TEXT,
                state TEXT, result TEXT, error TEXT, created REAL, updated REAL, UNIQUE(principal,request_id));
            CREATE TABLE IF NOT EXISTS events(cursor INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT, goal TEXT, task TEXT, kind TEXT, payload TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS created_resources(principal TEXT,goal TEXT,account TEXT,kind TEXT,resource TEXT,request TEXT,PRIMARY KEY(principal,goal,account,kind,resource));
            CREATE TABLE IF NOT EXISTS inbound_links(principal TEXT, goal TEXT, account INTEGER, peer TEXT, message TEXT,
                UNIQUE(principal,goal,account,peer,message));
            ''')
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    def _event(self, db, p, task, kind, payload):
        body = json.loads(task['body'])
        db.execute('INSERT INTO events(principal,goal,task,kind,payload,created) VALUES(?,?,?,?,?,?)',
            (p,body['goal_id'],task['id'],kind,canonical({**payload, 'goal_id':body['goal_id'], 'project_id':body['project_id'],
              'action_id':body['action_id'], 'account_id':body['account_id'], 'operation':body['operation']}),self.clock()))
    def set_authority(self, p, goal, authority):
        if not isinstance(authority.get('revision'),int) or isinstance(authority['revision'],bool) or authority['revision'] < 0:
            raise ValueError('revision must be a non-negative integer')
        if not isinstance(authority.get('enabled'),bool): raise ValueError('enabled must be boolean')
        import math
        expiry=float(authority.get('expires_at',0))
        authority={**authority,'expires_at':expiry}
        if not math.isfinite(expiry): raise ValueError('invalid expiry')
        for grant in authority.get('grants',[]):
            if not isinstance(grant.get('scope',{}).get('include_goal_created',False),bool): raise ValueError('scope include_goal_created must be boolean')
        if authority['enabled'] and not self.clock() < expiry <= self.clock()+65: raise ValueError('authority lease must expire within 65 seconds')
        if not isinstance(authority.get('grants'),list): raise ValueError('grants required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM authority WHERE principal=? AND goal=?',(p,goal)).fetchone()
            if old and authority['revision'] < old['revision']: raise ValueError('stale authorization revision')
            if old and authority['revision']==old['revision']:
                previous=json.loads(old['body']); previous.pop('expires_at',None)
                current=dict(authority); current.pop('expires_at',None)
                if previous!=current: raise ValueError('revision conflict')
            db.execute('INSERT OR REPLACE INTO authority VALUES(?,?,?,?)',(p,goal,authority['revision'],canonical(authority)))
            for task in db.execute("SELECT * FROM tasks WHERE principal=? AND state='queued'",(p,)).fetchall():
                if json.loads(task['body'])['goal_id'] != goal: continue
                try: self.validate(db,p,json.loads(task['body']))
                except ValueError:
                    db.execute("UPDATE tasks SET state='cancelled',error='authorization revoked',updated=? WHERE id=?",(self.clock(),task['id']))
                    self._event(db,p,task,'task.cancelled',{'reason':'authorization revoked'})
        return authority
    def validate(self, db, p, body):
        row=db.execute('SELECT body FROM authority WHERE principal=? AND goal=?',(p,body['goal_id'])).fetchone()
        a=json.loads(row['body']) if row else {}
        if not a.get('enabled') or a.get('expires_at',0)<=self.clock() or a.get('revision')!=body['authorization_revision']:
            raise ValueError('authorization inactive, expired or changed')
        if a.get('project_id') != body['project_id']: raise ValueError('goal/project binding mismatch')
        grant=next((g for g in a['grants'] if str(g['account_id'])==str(body['account_id']) and body['operation'] in g['operations']),None)
        if not grant: raise ValueError('account/operation not granted')
        expiry=grant.get('expires_at')
        if expiry is not None:
            try:
                expiry=float(expiry)
            except (ValueError,TypeError):
                from datetime import datetime
                expiry=datetime.fromisoformat(str(expiry).replace('Z','+00:00')).timestamp()
            import math
            if not math.isfinite(expiry) or expiry<=self.clock(): raise ValueError('account grant expired')
        if body['operation'] in {'message.read','dm.reply'}:
            peer=str(body['input'].get('conversation_id',''))
            if not peer or peer not in [str(x) for x in grant.get('scope',{}).get('conversation_ids',[])]: raise ValueError('conversation outside scope')
        if body['operation'] in {'content.publish','comment.reply','material.read'}:
            target=str(body['input'].get('publish_job_id' if body['operation'] in {'content.publish','material.read'} else 'note_id',''))
            kind='publish_job'
            if body['operation']=='content.publish' and not target:
                target=str(body['input'].get('draft_id',''));kind='draft'
            created=grant.get('scope',{}).get('include_goal_created') is True and body['operation']=='content.publish' and db.execute('SELECT 1 FROM created_resources WHERE principal=? AND goal=? AND account=? AND kind=? AND resource=?',(p,body['goal_id'],str(body['account_id']),kind,target)).fetchone()
            if not target or (not created and target not in [str(x) for x in grant.get('scope',{}).get('resource_ids',[])]): raise ValueError('resource outside scope')
            if body['operation']=='comment.reply' and str(body['input'].get('target_comment_id','')) not in [str(x) for x in grant.get('scope',{}).get('conversation_ids',[])]: raise ValueError('comment outside scope')
    def submit(self,p,body):
        revision=body.get('authorization_revision')
        if not isinstance(revision,int) or isinstance(revision,bool) or revision<0: raise ValueError('authorization_revision must be a non-negative integer')
        if body['operation'] not in SUPPORTED: raise ValueError(UNAVAILABLE.get(body['operation'],'operation unsupported'))
        digest=hashlib.sha256(canonical(body).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM tasks WHERE principal=? AND request_id=?',(p,body['request_id'])).fetchone()
            if old:
                if old['fingerprint']!=digest: raise ValueError('request_id fingerprint conflict')
                return self.serialize(old)
            self.validate(db,p,body)
            task_id=str(uuid.uuid4()); now=self.clock()
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',(task_id,p,body['request_id'],digest,canonical(body),'queued','{}','',now,now))
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            self._event(db,p,task,'task.queued',{})
            return self.serialize(task)
    def serialize(self,row):
        return {**json.loads(row['body']),'id':row['id'],'state':row['state'],'status':row['state'],'origin':'headquarters','result':json.loads(row['result']),
                'error':row['error'],'created_at':row['created'],'updated_at':row['updated']}
    def get(self,p,task_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM tasks WHERE principal=? AND id=?',(p,task_id)).fetchone()
            if not row: raise KeyError('task not found')
            return self.serialize(row)
    def events(self,p,after=0,goal=None):
        with self.connect() as db:
            rows=db.execute('SELECT * FROM events WHERE principal=? AND cursor>? ORDER BY cursor LIMIT 200',(p,after)).fetchall()
            value={'events':[{'cursor':r['cursor'],'task_id':r['task'],'goal_id':r['goal'],'project_id':json.loads(r['payload']).get('project_id'),'action_id':json.loads(r['payload']).get('action_id'),'account_id':json.loads(r['payload']).get('account_id'),'kind':r['kind'],
                'payload':json.loads(r['payload']),'created_at':r['created']} for r in rows if not goal or r['goal']==goal],
                'next_cursor':rows[-1]['cursor'] if rows else after}
            return {**value,'items':value['events'],'cursor':value['next_cursor']}
    def recover(self):
        with self.connect() as db:
            for row in db.execute("SELECT * FROM tasks WHERE state='running'").fetchall():
                op=json.loads(row['body'])['operation']; state='uncertain' if op in {'draft.create','content.publish','comment.reply','content.analyze','dm.reply'} else 'queued'
                db.execute('UPDATE tasks SET state=?,error=?,updated=? WHERE id=?',(state,'interrupted; reconcile local write' if state=='uncertain' else '',self.clock(),row['id']))
                self._event(db,row['principal'],row,'task.'+state,{})
    def tick(self, execute, principal_check):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row: return False
            body=json.loads(row['body'])
            try: self.validate(db,row['principal'],body); principal_check(row['principal'],body)
            except (ValueError,KeyError):
                db.execute("UPDATE tasks SET state='cancelled',error='authorization/account unavailable',updated=? WHERE id=?",(self.clock(),row['id']))
                self._event(db,row['principal'],row,'task.cancelled',{}); return True
            db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?",(self.clock(),row['id']))
            self._event(db,row['principal'],row,'task.running',{})
        try:
            # Recheck immediately before entering the workbench operation.
            with self.connect() as db: self.validate(db,row['principal'],body)
            principal_check(row['principal'],body)
            result=execute(row['principal'],body); state=result.pop('_pending', 'succeeded'); error=''
        except Exception as exc:
            from backend.app.services.business_operations import WriteUncertain
            result={}; state='uncertain' if isinstance(exc,WriteUncertain) or body['operation'] in {'draft.create','content.analyze'} else 'failed'; error='workbench operation not confirmed'
        with self.connect() as db:
            db.execute('UPDATE tasks SET state=?,result=?,error=?,updated=? WHERE id=?',(state,canonical(result),error,self.clock(),row['id']))
            self._event(db,row['principal'],row,'task.'+state,{'result':result,'error':error})
        return True

    def cancel(self,p,task_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM tasks WHERE principal=? AND id=?',(p,task_id)).fetchone()
            if not row: raise KeyError('task not found')
            if row['state']=='queued':
                db.execute("UPDATE tasks SET state='cancelled',error='cancelled by headquarters',updated=? WHERE id=?",(self.clock(),task_id))
                self._event(db,p,row,'task.cancelled',{})
        return self.get(p,task_id)
    def inbound(self,task,peer,message,payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            body=json.loads(task['body'])
            self.validate(db,task['principal'],body)
            if body['operation'] not in {'message.read','dm.reply'} or peer!=str(body['input'].get('conversation_id','')): raise ValueError('inbound outside bound conversation')
            result=db.execute('INSERT OR IGNORE INTO inbound_links VALUES(?,?,?,?,?)',(task['principal'],body['goal_id'],body['account_id'],peer,message))
            if result.rowcount: self._event(db,task['principal'],task,'message.inbound',payload)

    def complete_pending(self,task_id,state,result,error=''):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row or row['state'] not in ('waiting_model','waiting_platform'): return
            if row['state']==state: return
            db.execute('UPDATE tasks SET state=?,result=?,error=?,updated=? WHERE id=?',(state,canonical(result),error,self.clock(),task_id))
            self._event(db,row['principal'],row,'task.'+state,{'result':result,'error':error})

    def register_created(self,p,body,kind,resource):
        with self.connect() as db:
            db.execute('INSERT INTO created_resources VALUES(?,?,?,?,?,?)',(p,body['goal_id'],str(body['account_id']),kind,str(resource),body['request_id']))
