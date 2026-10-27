"""Conservative local screening; ordinary reviews and personal invitations stay eligible."""
import re

AD_TYPES = {'ad', 'ads', 'advertisement', 'sponsored'}
NON_NOTES = {'rec_query', 'hot_query'}
PITCH = re.compile(r'限时(?:优惠|特价|折扣)|优惠券|团购价|下单立减|购买链接|商品链接|橱窗下单|付费咨询|招生|招代理|招商加盟|课程报名|报名优惠|红娘服务|婚恋机构|包脱单')
ACTION = re.compile(r'私信|联系|咨询|下单|购买|扫码|加微|加v|vx|领取|报名|预约', re.I)
CRITICISM = re.compile(r'避坑|避雷|别买|不要买|警惕|维权|被骗|投诉')


def ad_reason(raw, title='', content=''):
    raw = raw if isinstance(raw, dict) else {}
    data = raw.get('data')
    if isinstance(data, dict):
        items = data.get('items')
        raw = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else data
    card = raw.get('note_card') or raw.get('note') or raw
    card = card if isinstance(card, dict) else {}
    for obj in (raw, card):
        if str(obj.get('model_type', '')).lower() in AD_TYPES:
            return '平台标记的广告'
        if any(obj.get(key) is True or type(obj.get(key)) is int and obj[key] == 1
               or isinstance(obj.get(key), str) and obj[key].lower() in ('true', '1')
               for key in ('is_ad', 'is_ads', 'is_sponsored')):
            return '平台标记的广告'
        if any(isinstance(obj.get(key), dict) and any(obj[key].get(field)
               for field in ('ad_id', 'ads_id', 'creative_id')) for key in ('ad_info', 'ads_info')):
            return '平台广告投放信息'
    text = re.sub(r'\s+', '', title + '\n' + content)
    if PITCH.search(text) and ACTION.search(text) and not CRITICISM.search(text):
        return '正文包含明确销售宣传和交易引导'
    return ''


def record(diagnostics, note, outcome, reason):
    counts = diagnostics.setdefault('counts', {})
    counts[outcome] = counts.get(outcome, 0) + 1
    rows = diagnostics.setdefault('records', [])
    if len(rows) < 100:
        rows.append({key: str(note.get(key) or '')[:160] for key in ('note_id', 'title', 'author_name')}
                    | {'outcome': outcome, 'reason': str(reason)[:500]})
