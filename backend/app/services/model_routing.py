"""Model execution uses the existing source credential and Multica-backed Channel routes."""
from pathlib import Path
from sqlalchemy import select
from fastapi import HTTPException
from backend.app.models import ModelConfig
from backend.app.services.channel_client import ChannelClient, ChannelError

CONFIG = Path(__file__).resolve().parents[3] / '.local/web/channel.json'
PROVIDER = 'multica-channel'
PURPOSES = {'text': ('content_analysis', 'manual'), 'image': ('image_analysis', 'manual'), 'dm': ('reply_draft', 'dm'), 'comment': ('lifestyle_editor', 'manual'), 'content': ('content_analysis', 'content')}


def channel_client():
    return ChannelClient(CONFIG)


def available_route(route, purpose):
    category, surface = PURPOSES[purpose]
    return ('*' in route['categories'] or category in route['categories']) and \
        ('*' in route['surfaces'] or surface in route['surfaces']) and \
        ('*' in route['event_types'] or ({'dm': 'dm.received', 'content': 'content.rewrite_requested'}.get(purpose, 'manual.request')) in route['event_types']) and \
        ('text' in route['kinds'] if purpose != 'image' else 'mixed' in route['kinds'])


def account_keys(account):
    return {str(account.id), f'user:{account.user_id}:account:{account.external_user_id or account.id}'}


def account_route(route, account):
    selectors = route.get('account_ids', ['*'])
    return '*' in selectors or bool(account_keys(account).intersection(selectors))


def catalog_agents(catalog):
    if 'agents' in catalog:
        return catalog['agents']
    # Compatibility for older Channel catalogs and synthetic tests only.
    return list({r['agent']['id']: r['agent'] for r in catalog.get('routes', []) if r.get('agent') and r['agent'].get('id')}.values())


def agent_choices(catalog, purpose, account):
    choices = []
    for agent in catalog_agents(catalog):
        route = next((r for r in catalog.get('routes', []) if r.get('agent') and r['agent']['id'] == agent['id']
                      and available_route(r, purpose) and account_route(r, account)), None)
        choices.append({'id': agent['id'], 'name': agent['name'], 'route_id': route['id'] if route else ''})
    return choices


def resolve_agent(catalog, purpose, account, agent_id='', route_id=''):
    candidates = [r for r in catalog.get('routes', []) if r.get('agent') and available_route(r, purpose) and account_route(r, account)]
    if agent_id:
        candidates = [r for r in candidates if r['agent']['id'] == agent_id]
    if route_id:
        candidates = [r for r in candidates if r['id'] == route_id]
    if not candidates and agent_id and not route_id and 'agents' in catalog:
        if not any(a['id'] == agent_id for a in catalog_agents(catalog)):
            raise HTTPException(400, '所选 Agent 已不在实时目录中，请刷新')
        identity = str(account.id) if purpose == 'comment' else f'user:{account.user_id}:account:{account.external_user_id or account.id}'
        result = channel_client().request('POST', '/v1/bindings', {'agent_id': agent_id, 'account_id': identity, 'purpose': purpose})
        route = {**result['route'], 'agent': result['agent']}
        candidates = [route]
    if not candidates or not (agent_id or route_id):
        raise HTTPException(400, '请选择当前账号和用途可用的 Agent')
    route = candidates[0]
    return route, {'id': route['agent']['id'], 'name': route['agent']['name'], 'route_id': route['id'], 'route_version': route.get('version')}


def validate_route(route_id, purpose, account=None, agent_id=''):
    try:
        catalog = channel_client().catalog()
    except ChannelError as exc:
        raise HTTPException(503, str(exc)) from None
    if account is not None:
        try:
            return resolve_agent(catalog, purpose, account, agent_id, route_id)[0]
        except ChannelError as exc:
            raise HTTPException(503, str(exc)) from None
    route = next((r for r in catalog['routes'] if r['id'] == route_id and available_route(r, purpose)), None)
    if not route or not route.get('agent'):
        raise HTTPException(400, '请选择该用途可用的 Multica 路由')
    return route


def default_config(db, user_id, purpose):
    return db.scalars(select(ModelConfig).where(ModelConfig.user_id == user_id,
        ModelConfig.model_type == purpose, ModelConfig.is_default.is_(True))).first()


def account_default(catalog, purpose, account):
    if purpose == 'dm':
        fast = [r for r in catalog.get('routes', []) if r.get('enabled',True)
                and r.get('execution_mode') == 'frozen_reply' and available_route(r,purpose)
                and '*' not in r.get('account_ids',[]) and account_keys(account).intersection(r.get('account_ids',[]))]
        if fast:
            priority=max(r.get('priority',50) for r in fast)
            fast=[r for r in fast if r.get('priority',50)==priority]
        if len(fast) == 1:
            return resolve_agent(catalog,purpose,account,route_id=fast[0]['id'])
    raise HTTPException(400, '请在智能大脑为当前账号选择并保存该用途的 Agent，可选择目录中的任意 Agent')


def ensure_account_config(db, account, purpose, catalog=None):
    catalog = catalog or channel_client().catalog()
    if purpose == 'comment':
        # Proactive comments use the existing text Agent selection. This is an
        # execution route for the same Agent, not another user-facing setting.
        selected = ensure_account_config(db, account, 'text', catalog)
        route, snapshot = resolve_agent(catalog, purpose, account, selected._agent_snapshot['id'])
        config = ModelConfig(user_id=account.user_id, model_type=purpose, provider=PROVIDER,
                            base_url='account:' + str(account.id), encrypted_api_key='',
                            name=snapshot['name'], model_name=route['id'], is_default=False)
        config._agent_snapshot = snapshot
        return config
    config = db.scalars(select(ModelConfig).where(ModelConfig.user_id == account.user_id,
        ModelConfig.model_type == purpose, ModelConfig.provider == PROVIDER,
        ModelConfig.base_url == 'account:' + str(account.id))).first()
    if config:
        try:
            route, snapshot = resolve_agent(catalog, purpose, account, route_id=config.model_name)
            config._agent_snapshot = snapshot
            return config
        except HTTPException:
            raise HTTPException(400, '已保存的 Agent 绑定不可用，请在智能大脑重新选择并保存') from None
    route, snapshot = account_default(catalog, purpose, account)
    if config is None:
        config = ModelConfig(user_id=account.user_id, model_type=purpose, provider=PROVIDER,
                             base_url='account:' + str(account.id), encrypted_api_key='', is_default=True)
        db.add(config)
    config.name, config.model_name = snapshot['name'], route['id']
    config._agent_snapshot = snapshot
    db.flush()
    return config
