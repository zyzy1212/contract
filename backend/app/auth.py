from dataclasses import dataclass, field
from typing import Annotated, AbstractSet, Literal

from fastapi import Depends, Header, HTTPException

from app.common.errors import InputValidationError, TenantAccessError


@dataclass(frozen=True)
class Actor:
    user_id: str
    tenant_id: str
    role: Literal["customer", "admin"]
    allowed_tenants: AbstractSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise InputValidationError("user_id must not be empty")
        if not self.tenant_id.strip():
            raise InputValidationError("tenant_id must not be empty")
        if any(not tenant_id.strip() for tenant_id in self.allowed_tenants):
            raise InputValidationError("allowed_tenants must not contain empty values")
        object.__setattr__(self, "allowed_tenants", frozenset(self.allowed_tenants))


def require_tenant(actor: Actor, requested_tenant_id: str | None = None) -> str:
    if requested_tenant_id is not None and not requested_tenant_id.strip():
        raise InputValidationError("requested_tenant_id must not be empty")
    target = requested_tenant_id or actor.tenant_id
    if actor.role == "customer" and target != actor.tenant_id:
        raise TenantAccessError("cross-tenant access denied")
    if (
        actor.role == "admin"
        and target != actor.tenant_id
        and target not in actor.allowed_tenants
    ):
        raise TenantAccessError("tenant is not assigned to administrator")
    if actor.role not in {"customer", "admin"}:
        raise TenantAccessError("unknown actor role")
    return target


def current_actor(
    x_actor_user: Annotated[str | None, Header(alias="X-Actor-User")] = None,
    x_actor_tenant: Annotated[str | None, Header(alias="X-Actor-Tenant")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
) -> Actor:
    """MVP identity mapping; production replaces header claims with OAuth tokens."""
    if (
        not x_actor_user
        or not x_actor_tenant
        or x_actor_role not in {"customer", "admin"}
    ):
        raise HTTPException(status_code=401, detail="missing or invalid actor identity")
    return Actor(
        user_id=x_actor_user,
        tenant_id=x_actor_tenant,
        role=x_actor_role,  # type: ignore[arg-type]
    )


def require_admin(actor: Actor = Depends(current_actor)) -> Actor:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="administrator role required")
    return actor
