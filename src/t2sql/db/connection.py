"""DB connection helper.

Hands out `app_owner` or `app_readonly` connections based on `DATABASE_URL_OWNER`
/ `DATABASE_URL_READONLY`. Refuses owner connections requested from
`t2sql.generation` or `t2sql.clarify` — those packages should only ever touch
the database through the readonly role.
"""

from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
from typing import Iterator, Literal

import psycopg
from dotenv import load_dotenv

load_dotenv()

Role = Literal["owner", "readonly"]

_RESTRICTED_PACKAGES = ("t2sql.generation", "t2sql.clarify")

_ENV_VARS: dict[Role, str] = {
    "owner": "DATABASE_URL_OWNER",
    "readonly": "DATABASE_URL_READONLY",
}


class OwnerConnectionForbidden(PermissionError):
    """Raised when generation/clarify code asks for an owner connection."""


def _caller_module() -> str | None:
    this_module = __name__
    for frame_info in inspect.stack():
        module = inspect.getmodule(frame_info.frame)
        if module is None:
            continue
        name = module.__name__
        if name == this_module or name.startswith("contextlib"):
            continue
        return name
    return None


def _database_url(role: Role) -> str:
    env_var = _ENV_VARS[role]
    url = os.environ.get(env_var)
    if not url:
        raise RuntimeError(f"{env_var} is not set (check your .env file)")
    return url


@contextmanager
def get_connection(role: Role = "readonly") -> Iterator[psycopg.Connection]:
    if role == "owner":
        caller = _caller_module()
        if caller and caller.startswith(_RESTRICTED_PACKAGES):
            raise OwnerConnectionForbidden(
                f"{caller} may not request an owner connection; use role='readonly' instead"
            )

    conn = psycopg.connect(_database_url(role))
    try:
        yield conn
    finally:
        conn.close()
