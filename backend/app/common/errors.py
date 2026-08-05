"""Typed errors shared across application boundaries."""


class DomainError(Exception):
    """Base class for expected application failures."""


class TenantAccessError(DomainError, PermissionError):
    """An actor attempted to access an unassigned tenant."""


class InputValidationError(DomainError, ValueError):
    """Actor-supplied identity or tenant context is malformed."""


class InfrastructureError(DomainError, RuntimeError):
    """A required infrastructure dependency is unavailable."""


class ConfigurationError(InfrastructureError):
    """Application or tenant context configuration is invalid."""


class KnowledgeConflict(DomainError, ValueError):
    """Existing knowledge content has different immutable provenance."""
