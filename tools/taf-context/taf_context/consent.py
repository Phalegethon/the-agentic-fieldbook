"""Immutable, exact-action authorization records for context operations."""

from __future__ import annotations

from dataclasses import dataclass
from .models import ContextAction


class ConsentError(ValueError):
    """Raised when a consent ledger does not match its strict wire schema."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid consent field: {field}")


Grant = tuple[ContextAction, str, str, str]

_GRANT_FIELDS = frozenset(
    {"action", "repository_identity", "provider_name", "granted_at"}
)


@dataclass(frozen=True)
class AuthorizationLedger:
    """A canonical, immutable set of exact action-and-scope grants.

    A grant authorizes one :class:`ContextAction` for one repository and one
    provider.  No action, repository, or provider is inferred from another
    grant.  Grants are held as tuples so callers cannot mutate the ledger.
    """

    grants: tuple[Grant, ...] = ()

    def __post_init__(self) -> None:
        if type(self.grants) is not tuple:
            raise ConsentError("grants")

        normalized: set[Grant] = set()
        for index, grant in enumerate(self.grants):
            normalized.add(_parse_grant_tuple(grant, f"grants[{index}]"))
        ordered = tuple(sorted(normalized, key=_grant_sort_key))
        object.__setattr__(self, "grants", ordered)

    def authorize(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_name: str,
        granted_at: str,
    ) -> "AuthorizationLedger":
        """Return a new ledger with one exact grant added idempotently."""
        grant = _parse_grant_tuple(
            (action, repository_identity, provider_name, granted_at), "grant"
        )
        return AuthorizationLedger(self.grants + (grant,))

    def is_authorized(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_name: str,
    ) -> bool:
        """Return whether this exact action and scope have been granted."""
        normalized_action = _parse_action(action, "action")
        _require_scope(repository_identity, "repository_identity")
        _require_scope(provider_name, "provider_name")
        return any(
            grant[0] is normalized_action
            and grant[1] == repository_identity
            and grant[2] == provider_name
            for grant in self.grants
        )

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AuthorizationLedger":
        """Build a ledger from the strict ``{"grants": [...]}`` schema."""
        if type(value) is not dict or set(value) != {"grants"}:
            raise ConsentError("ledger")
        entries = value["grants"]
        if type(entries) is not list:
            raise ConsentError("grants")

        grants: list[Grant] = []
        for index, entry in enumerate(entries):
            if type(entry) is not dict or set(entry) != _GRANT_FIELDS:
                raise ConsentError(f"grants[{index}]")
            grants.append(
                _parse_grant_tuple(
                    (
                        entry["action"],
                        entry["repository_identity"],
                        entry["provider_name"],
                        entry["granted_at"],
                    ),
                    f"grants[{index}]",
                )
            )
        return cls(tuple(grants))

    def to_dict(self) -> dict[str, object]:
        """Return canonical JSON-ready grants sorted by action and scope."""
        return {
            "grants": [
                {
                    "action": action.value,
                    "repository_identity": repository_identity,
                    "provider_name": provider_name,
                    "granted_at": granted_at,
                }
                for action, repository_identity, provider_name, granted_at in self.grants
            ]
        }


def _parse_grant_tuple(value: object, field: str) -> Grant:
    if type(value) is not tuple or len(value) != 4:
        raise ConsentError(field)
    action, repository_identity, provider_name, granted_at = value
    return (
        _parse_action(action, f"{field}.action"),
        _require_scope(repository_identity, f"{field}.repository_identity"),
        _require_scope(provider_name, f"{field}.provider_name"),
        _require_scope(granted_at, f"{field}.granted_at"),
    )


def _parse_action(value: object, field: str) -> ContextAction:
    if isinstance(value, ContextAction):
        return value
    if isinstance(value, str):
        try:
            return ContextAction(value)
        except ValueError as exc:
            raise ConsentError(field) from exc
    raise ConsentError(field)


def _require_scope(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConsentError(field)
    return value


def _grant_sort_key(grant: Grant) -> tuple[str, str, str, str]:
    action, repository_identity, provider_name, granted_at = grant
    return (action.value, repository_identity, provider_name, granted_at)
