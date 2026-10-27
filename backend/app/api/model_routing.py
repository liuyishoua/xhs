from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import ModelConfig, User, PlatformAccount
from backend.app.services.channel_client import ChannelError
from backend.app.services.model_routing import PROVIDER, PURPOSES, available_route, channel_client, default_config, validate_route, agent_choices, account_route, account_keys, resolve_agent, ensure_account_config

router = APIRouter(prefix='/model-routing', tags=['model-routing'])


class BindingRequest(BaseModel):
    route_id: str = Field(default='', max_length=80)
    agent_id: str = Field(default='', max_length=80)
    account_id: int | None = None


def owned_account(db, user, account_id):
    account = db.get(PlatformAccount, account_id) if account_id else None
    if not account or account.user_id != user.id or account.platform != 'xhs':
        raise HTTPException(404, '小红书账号不存在')
    return account


def user_routes(catalog, user, db, account=None):
    accounts = [account] if account else db.scalars(select(PlatformAccount).where(
        PlatformAccount.user_id == user.id, PlatformAccount.platform == 'xhs')).all()
    keys = {'user:' + str(user.id)}
    for item in accounts:
        keys.update(account_keys(item))
    routes = []
    for route in catalog.get('routes', []):
        selectors = route.get('account_ids', ['*'])
        visible = [value for value in selectors if value == '*' or value in keys]
        if visible:
            routes.append({**route, 'account_ids': visible})
    return routes


@router.get('')
def overview(user: User = Depends(get_current_user), db: Session = Depends(get_db), account_id: int | None = Query(None)):
    account = owned_account(db, user, account_id) if account_id else None
    bindings = {}
    for purpose in PURPOSES:
        config = (db.scalars(select(ModelConfig).where(ModelConfig.user_id == user.id, ModelConfig.model_type == purpose, ModelConfig.provider == PROVIDER, ModelConfig.base_url == 'account:' + str(account.id))).first() if account else default_config(db, user.id, purpose))
        bindings[purpose] = config.model_name if config and config.provider == PROVIDER and (account is None or config.base_url == 'account:' + str(account.id)) else None
    try:
        catalog = channel_client().catalog()
        default_errors = {}
        if account is not None:
            for purpose in PURPOSES:
                try:
                    config = ensure_account_config(db, account, purpose, catalog)
                    bindings[purpose] = config.model_name
                except HTTPException as exc:
                    default_errors[purpose] = exc.detail
            db.commit()
        if account is not None:
            catalog = channel_client().catalog()
        catalog = {**catalog, 'routes': user_routes(catalog, user, db, account)}
        return {**catalog, 'default_errors': default_errors, 'agent_choices': {p: agent_choices(catalog, p, account) for p in PURPOSES} if account else {}, 'bindings': bindings, 'choices': {p: [r['id'] for r in catalog['routes'] if available_route(r, p) and r.get('agent')] for p in PURPOSES}}
    except ChannelError as exc:
        return {'connected': False, 'error': str(exc), 'routes': [], 'choices': {}, 'bindings': bindings, 'multica_url': 'https://d.58nft.net:9447'}


@router.put('/{purpose}')
def bind(purpose: str, data: BindingRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if purpose not in PURPOSES:
        raise HTTPException(404, '用途不存在')
    account = owned_account(db, user, data.account_id) if data.account_id else None
    if data.agent_id and account is None:
        raise HTTPException(400, '请先选择账号')
    route = validate_route(data.route_id, purpose, account, data.agent_id)
    if account is None and not user_routes({'routes': [route]}, user, db):
        raise HTTPException(404, '该路由不属于当前用户的社交矩阵')
    configs = db.scalars(select(ModelConfig).where(ModelConfig.user_id == user.id, ModelConfig.model_type == purpose)).all()
    binding_account = 'account:' + str(account.id) if account else ''
    config = next((x for x in configs if x.provider == PROVIDER and x.base_url == binding_account), None)
    for item in configs:
        if item.base_url == binding_account:
            item.is_default = False
    if config is None:
        config = ModelConfig(user_id=user.id, model_type=purpose, provider=PROVIDER)
        db.add(config)
    config.name = route['agent']['name']
    config.model_name = route['id']
    config.base_url = 'account:' + str(account.id) if account else ''
    config.encrypted_api_key = ''
    config.is_default = True
    db.commit()
    return {'purpose': purpose, 'route_id': route['id']}
