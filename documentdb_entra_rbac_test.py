#!/usr/bin/env python3
"""Data-plane RBAC tests for Azure DocumentDB MongoCluster with Entra ID.

This intentionally tests behavior that the ARM control-plane API currently
rejects, such as database-scoped readWrite users.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import DefaultAzureCredential
    from pymongo import MongoClient
    from pymongo.auth_oidc import (
        OIDCCallback,
        OIDCCallbackContext,
        OIDCCallbackResult,
    )
    from pymongo.errors import OperationFailure, PyMongoError
except ModuleNotFoundError as exc:
    missing = exc.name or "required package"
    print(
        f"Missing Python dependency: {missing}\n"
        "Install dependencies with: python3 -m pip install -r tests/documentdb/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


DOCUMENTDB_TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


@dataclass(frozen=True)
class Role:
    db: str
    role: str

    def as_bson(self) -> dict[str, str]:
        return {"db": self.db, "role": self.role}


class AzureIdentityTokenCallback(OIDCCallback):
    def __init__(self, credential: DefaultAzureCredential, dump_claims: bool) -> None:
        self.credential = credential
        self.dump_claims = dump_claims

    def fetch(self, context: OIDCCallbackContext) -> OIDCCallbackResult:
        token = self.credential.get_token(DOCUMENTDB_TOKEN_SCOPE)
        if self.dump_claims:
            print_json("token_claims", decode_jwt_claims(token.token))
        return OIDCCallbackResult(
            access_token=token.token,
            expires_in_seconds=max(float(token.expires_on - time.time()), 0.0),
        )


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {"error": "token is not a JWT"}

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
    claims = json.loads(decoded)
    keys = [
        "aud",
        "iss",
        "tid",
        "oid",
        "appid",
        "azp",
        "sub",
        "upn",
        "preferred_username",
        "name",
    ]
    return {key: claims[key] for key in keys if key in claims}


def print_json(label: str, value: Any) -> None:
    print(f"{label}:")
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def build_credential(args: argparse.Namespace) -> DefaultAzureCredential:
    if args.azure_client_id:
        os.environ["AZURE_CLIENT_ID"] = args.azure_client_id

    return DefaultAzureCredential(
        managed_identity_client_id=args.azure_client_id,
        exclude_interactive_browser_credential=not args.interactive,
    )


def build_client(args: argparse.Namespace) -> MongoClient:
    credential = build_credential(args)
    callback = AzureIdentityTokenCallback(credential, dump_claims=args.dump_token_claims)
    auth_properties = {"OIDC_CALLBACK": callback}

    uri = (
        f"mongodb+srv://{args.cluster_name}.global.mongocluster.cosmos.azure.com/"
    )
    return MongoClient(
        uri,
        appname="documentdb-entra-rbac-test",
        authMechanism="MONGODB-OIDC",
        authMechanismProperties=auth_properties,
        connectTimeoutMS=args.timeout_ms,
        serverSelectionTimeoutMS=args.timeout_ms,
        socketTimeoutMS=args.timeout_ms,
        tls=True,
        retryWrites=False,
    )


def command_ping(args: argparse.Namespace) -> int:
    with build_client(args) as client:
        result = client.admin.command({"ping": 1})
        print_json("ping", result)
    return 0


def command_token(args: argparse.Namespace) -> int:
    credential = build_credential(args)
    token = credential.get_token(DOCUMENTDB_TOKEN_SCOPE)
    print_json("token_claims", decode_jwt_claims(token.token))
    return 0


def command_list_users(args: argparse.Namespace) -> int:
    with build_client(args) as client:
        result = client.admin.command({"usersInfo": 1})
        print_json("usersInfo", result)
    return 0


def user_info(admin_db: Any, principal_object_id: str) -> dict[str, Any] | None:
    result = admin_db.command(
        {"usersInfo": {"user": principal_object_id, "db": "admin"}}
    )
    users = result.get("users", [])
    return users[0] if users else None


def seed_database(
    client: MongoClient, database: str, collection_name: str
) -> dict[str, Any]:
    collection = client[database][collection_name]
    result = collection.insert_one({"source": "documentdb-rbac-test-seed"})
    return {
        "database": database,
        "collection": collection_name,
        "inserted_id": str(result.inserted_id),
    }


def command_seed_database(args: argparse.Namespace) -> int:
    with build_client(args) as client:
        print_json(
            "seed_database",
            seed_database(client, args.database, args.collection_name),
        )
    return 0


def command_create_user(args: argparse.Namespace) -> int:
    roles = [Role(db=args.database, role=args.role)]
    for extra_role in args.extra_role:
        database, separator, role_name = extra_role.partition(":")
        if not separator or not database or not role_name:
            print(
                f"Invalid --extra-role value {extra_role!r}; expected db:role.",
                file=sys.stderr,
            )
            return 2
        roles.append(Role(db=database, role=role_name))
    custom_data = {
        "IdentityProvider": {
            "type": "MicrosoftEntraID",
            "properties": {
                "principalType": args.principal_type,
            },
        },
    }

    with build_client(args) as client:
        if args.ensure_database:
            print_json(
                "seed_database",
                seed_database(client, args.database, args.collection_name),
            )

        admin_db = client.admin
        existing = user_info(admin_db, args.principal_object_id)

        if existing and not args.update_existing:
            print_json("existing_user", existing)
            print(
                "User already exists. Re-run with --update-existing to replace roles/customData.",
                file=sys.stderr,
            )
            return 3

        command_name = "updateUser" if existing else "createUser"
        command = {
            command_name: args.principal_object_id,
            "roles": [role.as_bson() for role in roles],
            "customData": custom_data,
        }

        result = admin_db.command(command)
        print_json(command_name, result)
        print_json("usersInfo", user_info(admin_db, args.principal_object_id))
    return 0


def command_verify_write_scope(args: argparse.Namespace) -> int:
    with build_client(args) as client:
        allowed_collection = client[args.allowed_database][args.collection_name]
        denied_collection = client[args.denied_database][args.collection_name]

        allowed_result = allowed_collection.insert_one(
            {"source": "documentdb-rbac-test"}
        )
        print_json(
            "allowed_insert",
            {
                "database": args.allowed_database,
                "inserted_id": str(allowed_result.inserted_id),
            },
        )

        try:
            denied_collection.insert_one({"source": "documentdb-rbac-test"})
        except OperationFailure as exc:
            print_json(
                "denied_insert",
                {
                    "database": args.denied_database,
                    "ok": True,
                    "error": exc.details or str(exc),
                },
            )
            return 0

        print_json(
            "denied_insert",
            {
                "database": args.denied_database,
                "ok": False,
                "error": "Insert unexpectedly succeeded.",
            },
        )
    return 4


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cluster-name",
        default="secure-documentdb-drocx-test2",
        help="Azure DocumentDB MongoCluster name.",
    )
    parser.add_argument(
        "--azure-client-id",
        default=None,
        help="Optional managed identity or workload identity client ID.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Allow DefaultAzureCredential to open an interactive browser if needed.",
    )
    parser.add_argument(
        "--dump-token-claims",
        action="store_true",
        help="Print selected non-secret JWT claims during MongoDB OIDC authentication.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120000,
        help="MongoDB connect/server/socket timeout in milliseconds.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test Azure DocumentDB Entra ID data-plane RBAC with PyMongo.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ["token", "ping", "list-users"]:
        subparser = subparsers.add_parser(name)
        add_common_args(subparser)

    seed = subparsers.add_parser("seed-database")
    add_common_args(seed)
    seed.add_argument(
        "--database",
        default="appdb",
        help="Database to create by inserting a seed document.",
    )
    seed.add_argument(
        "--collection-name",
        default="rbac_seed",
        help="Collection used for the seed insert.",
    )

    create_user = subparsers.add_parser("create-user")
    add_common_args(create_user)
    create_user.add_argument(
        "--principal-object-id",
        required=True,
        help="Entra object ID to register as a DocumentDB user.",
    )
    create_user.add_argument(
        "--principal-type",
        default="securityPrincipal",
        help="IdentityProvider principalType sent to DocumentDB data-plane createUser.",
    )
    create_user.add_argument(
        "--database",
        default="appdb",
        help="Database for the role assignment.",
    )
    create_user.add_argument(
        "--role",
        default="readWrite",
        help="MongoDB role to assign.",
    )
    create_user.add_argument(
        "--extra-role",
        action="append",
        default=[],
        help="Additional role in db:role form. Can be specified more than once.",
    )
    create_user.add_argument(
        "--update-existing",
        action="store_true",
        help="Use updateUser when the user already exists.",
    )
    create_user.add_argument(
        "--ensure-database",
        action="store_true",
        help="Insert a seed document into --database before calling createUser/updateUser.",
    )
    create_user.add_argument(
        "--collection-name",
        default="rbac_seed",
        help="Collection used when --ensure-database is set.",
    )

    verify = subparsers.add_parser("verify-write-scope")
    add_common_args(verify)
    verify.add_argument(
        "--allowed-database",
        default="appdb",
        help="Database where insert should succeed.",
    )
    verify.add_argument(
        "--denied-database",
        default="otherdb",
        help="Database where insert should fail.",
    )
    verify.add_argument(
        "--collection-name",
        default="rbac_probe",
        help="Collection used for insert probes.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    commands = {
        "token": command_token,
        "ping": command_ping,
        "list-users": command_list_users,
        "seed-database": command_seed_database,
        "create-user": command_create_user,
        "verify-write-scope": command_verify_write_scope,
    }

    try:
        return commands[args.command](args)
    except ClientAuthenticationError as exc:
        print(f"Azure authentication failed: {exc}", file=sys.stderr)
        return 10
    except OperationFailure as exc:
        print_json("mongo_operation_failure", exc.details or str(exc))
        return 11
    except PyMongoError as exc:
        print(f"MongoDB operation failed: {exc}", file=sys.stderr)
        return 12


if __name__ == "__main__":
    raise SystemExit(main())
