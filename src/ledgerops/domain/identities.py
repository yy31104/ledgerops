"""Immutable, distinct domain IDs; wire IDs use canonical UUID text."""

from dataclasses import dataclass
from typing import Self
from uuid import UUID


@dataclass(frozen=True, slots=True)
class _UuidId:
    value: UUID

    def __post_init__(self) -> None:
        if type(self.value) is not UUID:
            raise TypeError("domain IDs must contain a UUID")

    @classmethod
    def parse(cls, value: object) -> Self:
        if not isinstance(value, str):
            raise TypeError("wire IDs must be strings")
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("UUID must use lowercase, hyphenated canonical text")
        return cls(parsed)

    def __str__(self) -> str:
        return str(self.value)


class WorkspaceId(_UuidId):
    __slots__ = ()


class ActorId(_UuidId):
    __slots__ = ()


class ClientId(_UuidId):
    __slots__ = ()


class AccountId(_UuidId):
    __slots__ = ()


class OperationId(_UuidId):
    __slots__ = ()


class BeneficiaryId(_UuidId):
    __slots__ = ()


class ProviderNamespaceId(_UuidId):
    __slots__ = ()
