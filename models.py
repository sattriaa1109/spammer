from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), nullable=False)
    session_cookies = Column(JSON, nullable=False)
    proxy = Column(String(255), nullable=True)
    user_agent = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Target(Base):
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True, index=True)
    url_post = Column(Text, nullable=False)
    status = Column(String(50), default="pending")  # pending, in_progress, success, failed
    created_at = Column(DateTime, default=datetime.utcnow)


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    target_id = Column(Integer, ForeignKey("targets.id", ondelete="SET NULL"), nullable=True)
    action_time = Column(DateTime, default=datetime.utcnow)
    message = Column(Text, nullable=False)
