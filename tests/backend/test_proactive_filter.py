"""Ads are screened before model calls; rejected cards do not consume the note quota."""
import pytest

from test_proactive_interactions import setup, Platform, start, state, configure
from test_proactive_discovery import discovery
from backend.app.services import proactive_interactions as service
from backend.app.services.proactive_filter import ad_reason


@pytest.mark.parametrize('raw,title,content', [
    ({'model_type':'ad'}, '', ''),
    ({'note_card':{'is_ad':True}}, '', ''),
    ({'is_ads':'1'}, '', ''),
    ({'ad_info':{'ad_id':'paid-placement'}}, '', ''),
    ({}, '健身课程报名优惠', '私信咨询，限时优惠'),
    ({}, '红娘服务包脱单', '联系咨询，预约服务'),
])
def test_clear_advertising_is_filtered(raw,title,content):
    assert ad_reason(raw,title,content)


@pytest.mark.parametrize('title,content', [
    ('真诚找对象', '欢迎带自我介绍私信，找到删帖'),
    ('我开始健身了', '买了运动鞋，分享一周的真实体验'),
    ('健身课程报名避坑', '不要买，私信咨询也不要轻信，大家警惕'),
    ('运动打卡第74天', '今天去公园走路，状态不错'),
])
def test_personal_invitations_reviews_and_criticism_are_preserved(title,content):
    assert not ad_reason({'model_type':'note','is_ad':False,'ad_info':{'is_ad':False}},title,content)


def test_clear_ads_and_query_cards_do_not_consume_search_quota(discovery, monkeypatch):
    client,_,_ = discovery
    ad=Platform.card('n0','peer0');ad['model_type']='ad'
    pitch=Platform.card('n1','peer1');pitch['note_card']['title']='健身课程报名优惠';pitch['note_card']['desc']='私信咨询'
    cards=[ad, {'model_type':'rec_query'}, pitch]+[Platform.card(f'n{i}',f'peer{i}') for i in range(2,5)]
    monkeypatch.setattr(Platform,'search_note',lambda *a,**kw:(True,'',{'data':{'items':cards}}))
    start(client,keyword='健身',browse_limit=3);service.tick()
    assert [note for note,_ in Platform.writes]==['n2','n3','n4']
    diag=state(client)['runs'][0]['diagnostics']
    assert diag['scanned']==6 and diag['prepared']==3
    assert diag['counts']['ad']==2 and diag['counts']['non_note']==1
    assert {r['note_id'] for r in diag['records'] if r['outcome']=='ad'}=={'n0','n1'}


def test_ads_found_in_full_text_are_skipped_before_comment_and_model_reads(discovery, monkeypatch):
    client,_,_ = discovery
    original=Platform.get_note_info
    reads=[]
    def detail(self,url):
        result=original(self,url)
        if '/n0' in url:result[2]['data']['items'][0]['note_card']['desc']='健身课程报名优惠，私信预约'
        return result
    preview=Platform.get_comment_preview
    def comments(self,url):
        reads.append(url)
        return preview(self,url)
    monkeypatch.setattr(Platform,'get_note_info',detail)
    monkeypatch.setattr(Platform,'get_comment_preview',comments)
    start(client,keyword='健身',browse_limit=2);service.tick()
    assert [note for note,_ in Platform.writes]==['n1','n2']
    assert all('/n0' not in url for url in reads)
    assert state(client)['runs'][0]['diagnostics']['counts']['ad']==1


def test_previous_interactions_are_replaced_by_fresh_notes_from_same_page(discovery):
    client,_,_ = discovery
    start(client,keyword='露营',browse_limit=1);service.tick()
    start(client,keyword='露营',browse_limit=3);service.tick()
    assert [note for note,_ in Platform.writes]==['n0','n1','n2','n3']
    diag=state(client)['runs'][0]['diagnostics']
    assert diag['prepared']==3 and diag['counts']['duplicate']==1


def test_filter_can_be_disabled_without_changing_other_strategies(discovery, monkeypatch):
    client,_,_ = discovery
    ad=Platform.card('n0','peer0');ad['model_type']='ad'
    monkeypatch.setattr(Platform,'search_note',lambda *a,**kw:(True,'',{'data':{'items':[ad]}}))
    start(client,keyword='露营',filter_ads=False);service.tick()
    result=state(client)
    assert [note for note,_ in Platform.writes]==['n0']
    assert result['strategies']['recommend']['filter_ads'] is False
    assert result['strategies']['outreach']['filter_ads'] is True


def test_agent_skip_reason_is_retained_for_each_selected_note(discovery,monkeypatch):
    client,_,_=discovery
    monkeypatch.setattr(service,'decide',lambda *a,**kw:{'note_id':'','text':'','reason':'对方条件与账号资料不匹配'})
    start(client,keyword='找对象',browse_limit=3);service.tick()
    result=state(client)
    diag=result['runs'][0]['diagnostics']
    assert result['runs'][0]['state']=='skipped' and not result['items']
    assert diag['counts']['ai_skipped']==3
    assert all(r['reason']=='对方条件与账号资料不匹配' for r in diag['records'] if r['outcome']=='ai_skipped')
