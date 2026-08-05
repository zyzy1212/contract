import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth import Actor, require_tenant
from app.common.errors import InputValidationError, TenantAccessError
from app.config import get_settings


_loop_session_factories: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    async_sessionmaker[AsyncSession],
] = WeakKeyDictionary()
_loop_session_factories_lock = threading.Lock()


def async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the current event loop.

    Celery workers may run one ``asyncio.run`` per task in separate threads.
    Sharing one async engine across those loops fails with "attached to a
    different loop", so each loop gets its own engine and session factory.
    """
    loop = asyncio.get_running_loop()
    with _loop_session_factories_lock:
        factory = _loop_session_factories.get(loop)
        if factory is None:
            engine = create_async_engine(
                get_settings().database_url,
                pool_pre_ping=True,
            )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            _loop_session_factories[loop] = factory
        return factory


async def bind_tenant(session: AsyncSession, tenant_id: str) -> None:
    if not tenant_id.strip():
        raise InputValidationError("tenant_id must not be empty")
    await session.execute(
        text("select set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


async def bind_actor_context(
    session: AsyncSession,
    actor: Actor,
    requested_tenant_id: str | None = None,
    *,
    enable_public_knowledge_write: bool = False,
) -> str:
    """Bind validated actor capabilities to transaction-local PostgreSQL settings."""
    tenant_id = require_tenant(actor, requested_tenant_id)
    if enable_public_knowledge_write and actor.role != "admin":
        raise TenantAccessError("public knowledge writes require an administrator")
    await bind_tenant(session, tenant_id)
    await session.execute(
        text("select set_config('app.knowledge_admin', :enabled, true)"),
        {"enabled": "true" if enable_public_knowledge_write else "false"},
    )
    return tenant_id


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    factory = async_session_factory()
    async with factory() as session, session.begin():
        await bind_tenant(session, tenant_id)
        yield session


@asynccontextmanager
async def tenant_transaction(
    actor: Actor,
    requested_tenant_id: str | None = None,
    *,
    enable_public_knowledge_write: bool = False,
) -> AsyncIterator[AsyncSession]:
    factory = async_session_factory()
    async with factory() as session, session.begin():
        await bind_actor_context(
            session,
            actor,
            requested_tenant_id,
            enable_public_knowledge_write=enable_public_knowledge_write,
        )
        yield session
