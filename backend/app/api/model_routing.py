from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import ModelConfig, User
from backend.app.services.channel_client import ChannelError
from backend.app.services.model_routing import PROVIDER, PURPOSES, available_route, channel_client, default_config, validate_route

router = APIRouter(prefix='/model-routing', tags=['model-routing'])


class BindingRequest(BaseModel):
    route_id: str = Field(min_length=1, max_length=80)


@router.get('')
def overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bindings = {}
    for purpose in PURPOSES:
        config = default_config(db, user.id, purpose)
        bindings[purpose] = config.model_name if config and config.provider == PROVIDER else None
    try:
        catalog = channel_client().catalog()
        return {**catalog, 'bindings': bindings, 'choices': {p: [r['id'] for r in catalog['routes'] if available_route(r, p) and r.get('agent')] for p in PURPOSES}}
    except ChannelError as exc:
        return {'connected': False, 'error': str(exc), 'routes': [], 'choices': {}, 'bindings': bindings, 'multica_url': 'https://d.58nft.net:9447'}


@router.put('/{purpose}')
def bind(purpose: str, data: BindingRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if purpose not in PURPOSES:
        raise HTTPException(404, '用途不存在')
    route = validate_route(data.route_id, purpose)
    configs = db.scalars(select(ModelConfig).where(ModelConfig.user_id == user.id, ModelConfig.model_type == purpose)).all()
    config = next((x for x in configs if x.provider == PROVIDER), None)
    for item in configs:
        item.is_default = False
    if config is None:
        config = ModelConfig(user_id=user.id, model_type=purpose, provider=PROVIDER)
        db.add(config)
    config.name = route['name']
    config.model_name = route['id']
    config.base_url = ''
    config.encrypted_api_key = ''
    config.is_default = True
    db.commit()
    return {'purpose': purpose, 'route_id': route['id']}
