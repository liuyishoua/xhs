"""Model execution uses the existing source credential and Multica-backed Channel routes."""
from pathlib import Path
from sqlalchemy import select
from fastapi import HTTPException
from backend.app.models import ModelConfig
from backend.app.services.channel_client import ChannelClient, ChannelError

CONFIG = Path(__file__).resolve().parents[3] / '.local/web/channel.json'
PROVIDER = 'multica-channel'
PURPOSES = {'text': ('content_analysis', 'manual'), 'image': ('image_analysis', 'manual'), 'dm': ('reply_draft', 'dm')}


def channel_client():
    return ChannelClient(CONFIG)


def available_route(route, purpose):
    category, surface = PURPOSES[purpose]
    return ('*' in route['categories'] or category in route['categories']) and \
        ('*' in route['surfaces'] or surface in route['surfaces']) and \
        ('*' in route['event_types'] or ('dm.received' if purpose == 'dm' else 'manual.request') in route['event_types']) and \
        ('text' in route['kinds'] if purpose != 'image' else 'mixed' in route['kinds'])


def validate_route(route_id, purpose):
    try:
        catalog = channel_client().catalog()
    except ChannelError as exc:
        raise HTTPException(503, str(exc)) from None
    route = next((r for r in catalog['routes'] if r['id'] == route_id and available_route(r, purpose)), None)
    if not route or not route.get('agent'):
        raise HTTPException(400, '请选择该用途可用的 Multica 路由')
    return route


def default_config(db, user_id, purpose):
    return db.scalars(select(ModelConfig).where(ModelConfig.user_id == user_id,
        ModelConfig.model_type == purpose, ModelConfig.is_default.is_(True))).first()
