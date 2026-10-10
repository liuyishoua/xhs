"""Account-scoped, durable inbound and outbound private-message state."""
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class DmListener(Base):
    __tablename__ = 'xhs_dm_listeners'
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    control_mode: Mapped[str] = mapped_column(String(24), default='auto_approve', server_default='auto_approve')
    route_id: Mapped[str] = mapped_column(String(80), default='')
    state: Mapped[str] = mapped_column(String(32), default='stopped')
    error: Mapped[str] = mapped_column(Text, default='')
    started_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)


class DirectMessage(Base):
    __tablename__ = 'xhs_direct_messages'
    __table_args__ = (UniqueConstraint('account_id', 'message_id', name='uq_xhs_dm_account_message'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'), index=True)
    message_id: Mapped[str] = mapped_column(String(160))
    peer_id: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    media: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    channel_payload: Mapped[dict] = mapped_column(JSON)
    channel_event_id: Mapped[str] = mapped_column(String(80), default='')
    state: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    reply_text: Mapped[str] = mapped_column(Text, default='')
    outbound_mid: Mapped[str] = mapped_column(String(80), default='')
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)


class DmHistory(Base):
    """Both directions, including history that must never trigger an automatic reply."""
    __tablename__ = 'xhs_dm_history'
    __table_args__ = (
        UniqueConstraint('account_id', 'peer_id', 'message_id', name='uq_xhs_dm_history_message'),
        Index('ix_xhs_dm_history_conversation_time', 'account_id', 'peer_id', 'sent_at'),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'))
    peer_id: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    media: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    content_type: Mapped[int] = mapped_column(Integer, default=1)
    sent_at: Mapped[object] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default='history')
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now)


class DmConversation(Base):
    __tablename__ = 'xhs_dm_conversations'
    __table_args__ = (UniqueConstraint('account_id', 'peer_id', name='uq_xhs_dm_conversation'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'))
    peer_id: Mapped[str] = mapped_column(String(128))
    history_status: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    session_state: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    updated_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)


class DmMedia(Base):
    __tablename__ = 'xhs_dm_media'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(120), default='')
    url: Mapped[str] = mapped_column(Text, default='')
    native: Mapped[dict] = mapped_column(JSON, default=dict)
    content_type: Mapped[int] = mapped_column(Integer, default=2)
    filename: Mapped[str] = mapped_column(String(100), default='')
    mime: Mapped[str] = mapped_column(String(40), default='')
    channel_asset_id: Mapped[str] = mapped_column(String(100), default='')
    library: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=shanghai_now)
