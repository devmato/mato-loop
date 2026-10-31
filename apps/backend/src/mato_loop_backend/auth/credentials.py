from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.models import Credential


class CredentialCipher:
    def __init__(self, key_path: Path | None = None) -> None:
        config = get_runtime_config()
        self.key_path = key_path or config.secrets_dir / "credential.key"
        self._key = self._load_or_create_key()
        self._fernet = Fernet(self._key)

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key + b"\n")
        self.key_path.chmod(0o600)
        return key

    def encrypt(self, payload: dict[str, Any]) -> bytes:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        return self._fernet.encrypt(serialized)

    def derive_key(self, context: bytes) -> bytes:
        return hmac.digest(self._key, context, "sha256")

    def decrypt(self, payload: bytes) -> dict[str, Any]:
        value = json.loads(self._fernet.decrypt(payload))
        if not isinstance(value, dict):
            raise ValueError("Credential payload must be an object")
        return value


async def store_credential(
    db: AsyncSession,
    *,
    owner_id: UUID | None,
    kind: str,
    label: str,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    credential: Credential | None = None,
    _cipher: CredentialCipher | None = None,
) -> Credential:
    cipher = _cipher or CredentialCipher()
    if credential is None:
        credential = Credential(
            user_id=owner_id,
            kind=kind,
            label=label,
            encrypted_payload=cipher.encrypt(payload),
            metadata_json=metadata or {},
        )
        db.add(credential)
    else:
        if credential.user_id != owner_id:
            raise ValueError("Credential scope mismatch")
        credential.kind = kind
        credential.label = label
        credential.encrypted_payload = cipher.encrypt(payload)
        credential.metadata_json = metadata or {}
    await db.flush()
    return credential


async def load_credential(
    db: AsyncSession,
    credential_id: UUID,
    *,
    owner_id: UUID | None,
    populate_existing: bool = False,
) -> Credential | None:
    credential = await db.get(
        Credential, credential_id, populate_existing=populate_existing
    )
    return (
        credential
        if credential is not None and getattr(credential, "user_id", object()) == owner_id
        else None
    )


def scoped_credentials(owner_id: UUID | None):
    return select(Credential).where(
        Credential.user_id.is_(None)
        if owner_id is None
        else Credential.user_id == owner_id
    )


async def delete_credential(
    db: AsyncSession, credential_id: UUID, *, owner_id: UUID | None
) -> bool:
    credential = await load_credential(db, credential_id, owner_id=owner_id)
    if credential is None:
        return False
    await db.delete(credential)
    return True


def read_credential(credential: Credential) -> dict[str, Any]:
    return CredentialCipher().decrypt(credential.encrypted_payload)
