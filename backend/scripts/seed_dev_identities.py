"""Seed the dev identities that the frontend role switcher sends as headers."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db import async_session_factory


IDENTITIES = (
    {
        "tenant_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "slug": "tenant-a",
        "name": "客户甲",
        "user": ("user-a", "user-a@example.test", "客户用户", "customer"),
    },
    {
        "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "slug": "law-firm",
        "name": "律所甲",
        "user": ("admin-a", "admin-a@example.test", "管理员", "admin"),
    },
)


async def seed() -> None:
    factory = async_session_factory()
    async with factory() as session, session.begin():
        for item in IDENTITIES:
            await session.execute(
                text(
                    """
                    INSERT INTO tenants (id, slug, name, status)
                    VALUES (
                        CAST(:id AS uuid), :slug, :name,
                        CAST('active' AS tenant_status)
                    )
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": item["tenant_id"],
                    "slug": item["slug"],
                    "name": item["name"],
                },
            )
            user_id, email, display_name, role = item["user"]
            await session.execute(
                text(
                    """
                    INSERT INTO users (
                        tenant_id, external_subject, email, display_name, role
                    ) VALUES (
                        CAST(:tenant_id AS uuid), :external_subject, :email,
                        :display_name, CAST(:role AS actor_role)
                    )
                    ON CONFLICT (tenant_id, external_subject) DO NOTHING
                    """
                ),
                {
                    "tenant_id": item["tenant_id"],
                    "external_subject": user_id,
                    "email": email,
                    "display_name": display_name,
                    "role": role,
                },
            )


if __name__ == "__main__":
    asyncio.run(seed())
    print(
        "seeded dev identities: "
        "tenant-a/user-a (aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa), "
        "law-firm/admin-a (bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb)"
    )
