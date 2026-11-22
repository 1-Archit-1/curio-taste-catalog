"""Operator CLI: user provisioning, addon token rotation, and keypair management.

Run with `python -m curio.cli <command>` or `curio <command>`.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from curio.crypto import generate_keypair
from curio.db.engine import to_sqlalchemy_url
from curio.seed import seed_demo
from curio.services import keys as keys_service
from curio.services import users as users_service
from curio.settings import Settings


@asynccontextmanager
async def _open_session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(to_sqlalchemy_url(database_url), poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _cmd_users_create(database_url: str, username: str) -> int:
    async with _open_session(database_url) as session:
        provisioned = await users_service.create_user_with_login_token(session, username=username)
        await session.commit()

    print(f"Created user: {provisioned.user.username} ({provisioned.user.id})")
    print(f"Login token (save now, shown once): {provisioned.login_token}")
    print(f"Addon manifest path: /{provisioned.user.addon_token}/manifest.json")
    return 0


async def _cmd_users_login_token(database_url: str, username: str) -> int:
    async with _open_session(database_url) as session:
        user, login_token = await users_service.issue_login_token(session, username=username)
        await session.commit()

    print(f"Issued login token for {user.username} ({user.id})")
    print(f"Login token (save now, shown once): {login_token}")
    return 0


async def _cmd_users_rotate_addon_token(database_url: str, username: str) -> int:
    async with _open_session(database_url) as session:
        user = await users_service.rotate_addon_token_for_username(session, username=username)
        await session.commit()

    print(f"Rotated addon token for {user.username} ({user.id})")
    print(f"New addon manifest path: /{user.addon_token}/manifest.json")
    return 0


async def _cmd_users_revoke_login_tokens(database_url: str, username: str) -> int:
    async with _open_session(database_url) as session:
        result = await users_service.revoke_login_tokens_for_username(session, username=username)
        await session.commit()
    print(f"Revoked login tokens for {result.user.username} ({result.user.id})")
    print(f"Login tokens revoked: {result.login_tokens_revoked}")
    print(f"Sessions deleted: {result.sessions_deleted}")
    return 0


async def _cmd_keys_rotate(
    database_url: str,
    old_private_key: str,
    new_public_key: str,
) -> int:
    async with _open_session(database_url) as session:
        count = await keys_service.rotate_all_keys(
            session,
            old_private_key=old_private_key,
            new_public_key=new_public_key,
        )
        await session.commit()

    print(f"Re-encrypted {count} stored API key(s) under the new keypair.")
    return 0


def _cmd_keygen() -> int:
    kp = generate_keypair()
    print(f"KEY_PUBLIC_KEY={kp.public_key}")
    print(f"KEY_PRIVATE_KEY={kp.private_key}")
    return 0


async def _cmd_seed_demo(database_url: str, username: str) -> int:
    async with _open_session(database_url) as session:
        result = await seed_demo(session, username=username)
        await session.commit()

    print(f"Seeded demo user: {result.user.username} ({result.user.id})")
    print(f"Addon manifest path: /{result.user.addon_token}/manifest.json")
    if result.login_token is not None:
        print(f"Login token (save now, shown once): {result.login_token}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curio", description="Curio operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="Generate a new Curve25519 keypair for sealed-box encryption")

    users_parser = sub.add_parser("users", help="Manage users and tokens")
    users_sub = users_parser.add_subparsers(dest="users_command", required=True)

    p_create = users_sub.add_parser("create", help="Create a user and print a login token")
    p_create.add_argument("username", help="Unique username")

    p_login = users_sub.add_parser(
        "create-login-token",
        help="Issue an additional login token for an existing user",
    )
    p_login.add_argument("username", help="Existing username")

    p_rotate_addon = users_sub.add_parser(
        "rotate-addon-token",
        help="Rotate a user's addon URL token (invalidates the old manifest URL)",
    )
    p_rotate_addon.add_argument("username", help="Existing username")

    p_revoke = users_sub.add_parser(
        "revoke-login-tokens",
        help="Revoke all of a user's login tokens and delete all of their sessions",
    )
    p_revoke.add_argument("username", help="Existing username")

    keys_parser = sub.add_parser("keys", help="Manage encryption keys")
    keys_sub = keys_parser.add_subparsers(dest="keys_command", required=True)

    p_rotate_keys = keys_sub.add_parser(
        "rotate",
        help="Re-encrypt all stored user API keys under a new keypair",
    )
    p_rotate_keys.add_argument(
        "--old-private-key",
        default=os.environ.get("OLD_KEY_PRIVATE_KEY") or os.environ.get("KEY_PRIVATE_KEY"),
        help="Current private key (defaults to OLD_KEY_PRIVATE_KEY or KEY_PRIVATE_KEY env var)",
    )
    p_rotate_keys.add_argument(
        "--new-public-key",
        default=os.environ.get("NEW_KEY_PUBLIC_KEY"),
        help="New public key (defaults to NEW_KEY_PUBLIC_KEY env var)",
    )

    p_seed = sub.add_parser(
        "seed-demo",
        help="Idempotently seed a demo user, sample media items, presets, and slots",
    )
    p_seed.add_argument(
        "--username",
        default="demo",
        help="Demo username (default: demo)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "keygen":
        return _cmd_keygen()

    settings = Settings()

    try:
        if args.command == "users":
            if args.users_command == "create":
                return asyncio.run(_cmd_users_create(settings.database_url, args.username))
            if args.users_command == "create-login-token":
                return asyncio.run(_cmd_users_login_token(settings.database_url, args.username))
            if args.users_command == "rotate-addon-token":
                return asyncio.run(
                    _cmd_users_rotate_addon_token(settings.database_url, args.username)
                )
            if args.users_command == "revoke-login-tokens":
                return asyncio.run(
                    _cmd_users_revoke_login_tokens(settings.database_url, args.username)
                )
        elif args.command == "keys" and args.keys_command == "rotate":
            if not args.old_private_key or not args.new_public_key:
                parser.error(
                    "keys rotate requires --old-private-key (or KEY_PRIVATE_KEY) "
                    "and --new-public-key (or NEW_KEY_PUBLIC_KEY)"
                )
            return asyncio.run(
                _cmd_keys_rotate(
                    settings.database_url,
                    old_private_key=args.old_private_key,
                    new_public_key=args.new_public_key,
                )
            )
        elif args.command == "seed-demo":
            return asyncio.run(_cmd_seed_demo(settings.database_url, args.username))
    except (
        users_service.UserAlreadyExistsError,
        users_service.UserNotFoundError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
