"""Continuous live history may use SQL; gaps must still synchronize the platform."""
from datetime import timedelta
from sqlalchemy import select
from test_direct_messages import database, empty_history
from backend.app.models import PlatformAccount
from backend.app.models.direct_message import DmHistory
from backend.app.core.time import shanghai_now
from backend.app.services import dm_history as history


def test_completed_live_history_extends_coverage_without_platform_fetch(database):
    now=shanghai_now()
    with database() as db:
        account=db.get(PlatformAccount,1)
        history.sync_history(db,account,'customer-a',empty_history,now)
        history.save_message(db,1,'customer-a','new-live','inbound','新的消息',now+timedelta(seconds=5),source='live')
        db.commit()
        def forbidden(*args,**kwargs):raise AssertionError('continuous live history must use SQL')
        status=history.sync_history(db,account,'customer-a',forbidden,now+timedelta(seconds=10),live_complete=True)
        assert status['read_source']=='live_sql' and status['pages']==0
        rows=history.history_rows(db,1,'customer-a',now-timedelta(hours=24),now+timedelta(seconds=10))
        assert [r.message_id for r in rows]==['new-live']


def test_missing_or_disconnected_history_still_fetches_platform(database):
    now=shanghai_now();calls=[]
    def fetch(*args,**kwargs):calls.append(True);return empty_history(*args,**kwargs)
    with database() as db:
        account=db.get(PlatformAccount,1)
        history.sync_history(db,account,'customer-a',fetch,now,live_complete=True)
        assert len(calls)==1
        history.sync_history(db,account,'customer-a',fetch,now+timedelta(seconds=10))
        assert len(calls)==2
        history.sync_history(db,account,'customer-a',fetch,now+timedelta(hours=25),live_complete=True)
        assert len(calls)==3


def test_requested_history_refresh_is_not_bypassed_by_live_cache(database):
    now=shanghai_now();calls=[]
    def fetch(*args,**kwargs):calls.append(True);return empty_history(*args,**kwargs)
    with database() as db:
        account=db.get(PlatformAccount,1)
        history.sync_history(db,account,'customer-a',fetch,now)
        history.sync_history(db,account,'customer-a',fetch,now+timedelta(seconds=10),
            live_complete=True,refresh_existing=True)
        assert len(calls)==2
