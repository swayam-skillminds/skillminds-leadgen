"""Secrets: never in the database, logs, bundles or exports (A.4, Phase 5.2).

A secret is looked up by name, in this order:

1. ``LEADSCRAPER_SECRET_<NAME>`` -- an environment variable (server mode,
   ``docker compose`` env files); the name upper-cased, non-alphanumerics
   turned into ``_``;
2. a file named ``<name>`` in ``LEADSCRAPER_SECRETS_DIR`` or ``/run/secrets``
   (Docker / Kubernetes mounted secrets);
3. the OS keychain via ``keyring`` (local mode), service ``leadscraper``.

Configs and notification targets refer to a secret by *name*
(``password_secret_name: smtp-password``), so what is stored and exported is
only ever the reference. ``scrub`` removes secret-looking values from documents
that leave the machine (bundles, config exports).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "leadscraper"
SECRETS_DIR_ENV = "LEADSCRAPER_SECRETS_DIR"
_DOCKER_SECRETS = Path("/run/secrets")
#: Key segments (split on ``_ - .``) that make a value a secret wherever it appears.
_SECRET_SEGMENTS = frozenset({"secret", "password", "passwd", "token", "credential",
                              "credentials", "dsn", "apikey"})
#: Key pairs that do the same ("api_key", "db_url", ...).
_SECRET_PAIRS = ("api_key", "private_key", "access_key", "db_url", "database_url",
                 "webhook_url", "service_account")
#: A reference to a secret by name is fine to store and export.
_REFERENCE_SUFFIX = "_secret_name"
REDACTED = "***"


def env_name(name: str) -> str:
    return "LEADSCRAPER_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _valid_name(name: str) -> bool:
    return bool(name) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name))


def _from_file(name: str) -> str | None:
    dirs = [Path(os.environ[SECRETS_DIR_ENV])] if os.environ.get(SECRETS_DIR_ENV) else []
    dirs.append(_DOCKER_SECRETS)
    for directory in dirs:
        path = directory / name
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _keyring():
    if os.environ.get("LEADSCRAPER_NO_KEYRING"):
        return None
    try:
        import keyring

        return keyring
    except Exception:  # pragma: no cover - keyring is a dependency, but be safe
        return None


def get_secret(name: str) -> str | None:
    """The secret called `name`, or None when it is not configured anywhere."""
    if not _valid_name(name):
        return None
    value = os.environ.get(env_name(name))
    if value:
        return value
    value = _from_file(name)
    if value:
        return value
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except Exception as exc:          # no backend (headless Linux): not configured
        log.debug("keyring unavailable for %s: %s", name, exc)
        return None


def secret_source(name: str) -> str | None:
    """Where `name` is configured (env / file / keyring), without revealing it."""
    if not _valid_name(name):
        return None
    if os.environ.get(env_name(name)):
        return "env"
    if _from_file(name):
        return "file"
    keyring = _keyring()
    try:
        if keyring is not None and keyring.get_password(SERVICE, name):
            return "keyring"
    except Exception:
        return None
    return None


def set_secret(name: str, value: str) -> None:
    """Store a secret in the OS keychain (local mode). Raises if there is none."""
    if not _valid_name(name):
        raise ValueError(f"invalid secret name {name!r}")
    keyring = _keyring()
    if keyring is None:
        raise RuntimeError("no OS keychain available; use an environment variable "
                           f"({env_name(name)}) or a secret file instead")
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> bool:
    keyring = _keyring()
    if keyring is None or not _valid_name(name):
        return False
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except Exception:
        return False


def is_secret_key(key: str) -> bool:
    key = str(key).lower()
    if key.endswith(_REFERENCE_SUFFIX):
        return False
    segments = re.split(r"[_.\-]", key)
    if _SECRET_SEGMENTS & set(segments):
        return True
    joined = "_".join(segments)
    return any(pair in joined for pair in _SECRET_PAIRS)


def scrub(value: Any) -> Any:
    """A copy of `value` with every secret-looking entry dropped.

    Keys ending in ``_secret_name`` are references (names), kept as they are.
    """
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not is_secret_key(k)}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def mask_url(url: str | None) -> str | None:
    """Scheme and host only: ``https://hooks.slack.com/***``."""
    if not url:
        return url
    match = re.match(r"^([a-z][a-z0-9+.-]*://)([^/@]*@)?([^/?#]+)", url, re.I)
    if not match:
        return REDACTED
    return f"{match.group(1)}{match.group(3)}/{REDACTED}"


def mask_db_url(url: str) -> str:
    """A database URL without its password."""
    return re.sub(r"(://[^:/@]+):[^@]+@", r"\1:***@", url)
