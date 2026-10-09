"""App-level secrets (API keys) in the OS keychain, next to the per-account Gmail passwords.

Environment variables always win, so cloud deployments keep using env/secret managers.
"""

import keyring

SERVICE = "email-agent"
_PREFIX = "app:"


def get(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, _PREFIX + name)
    except Exception:
        return None


def set(name: str, value: str) -> None:  # noqa: A001 - mirrors keyring's API
    keyring.set_password(SERVICE, _PREFIX + name, value)
