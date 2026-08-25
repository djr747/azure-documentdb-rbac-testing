# DocumentDB Entra RBAC Data-Plane Test

This test uses PyMongo `MONGODB-OIDC` with an Azure Identity token callback. It is intended to validate DocumentDB behavior that the ARM control-plane API currently rejects, such as `readWrite` scoped to `appdb`.

## Install

```sh
python3 -m venv .venv-documentdb
. .venv-documentdb/bin/activate
python3 -m pip install -r tests/documentdb/requirements.txt
```

## Authentication

For local human-user testing, sign in first:

```sh
az logout
az login --tenant 78a465d8-9ae4-44b5-a3b3-e027f58883a8 --scope "https://ossrdbms-aad.database.windows.net/.default"
```

If the secure cluster has public access disabled, run this from a host with private endpoint network and DNS access.

The current temporary validation cluster name is `secure-documentdb-drocx-test2`.

## Commands

Check the token claims that Azure Identity will send to DocumentDB:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py token
```

Ping the secure cluster as the current Azure identity:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py ping
```

Create `appdb` first by inserting a seed document:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py seed-database \
  --database appdb
```

Try creating the `workload-identity-test-eus1` identity as `readWrite` on `appdb`:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py create-user \
  --principal-object-id 16d922ac-481a-4590-9224-43260593adde \
  --principal-type securityPrincipal \
  --database appdb \
  --role readWrite \
  --ensure-database
```

If DocumentDB accepts the create but the user already exists, re-run with:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py create-user \
  --principal-object-id 16d922ac-481a-4590-9224-43260593adde \
  --principal-type securityPrincipal \
  --database appdb \
  --role readWrite \
  --update-existing
```

For a managed identity or workload identity runtime, pass the client ID used by that runtime:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py ping \
  --azure-client-id 297edc90-e4d5-4458-97a6-9abde739a33c
```

To test the documented non-root cluster-wide write combination:

```sh
python3 tests/documentdb/documentdb_entra_rbac_test.py create-user \
  --principal-object-id 262ced97-58f2-48d2-a171-0f71dfbc9690 \
  --principal-type securityPrincipal \
  --database admin \
  --role readWriteAnyDatabase \
  --extra-role admin:clusterAdmin
```

This grants broad cluster-level privileges. Do not run it unless that access is intentionally approved for the target principal.

## Live Results

Validated with `drobson@drocx.com` as the Entra admin. Exact commands, source links, and full error payloads are captured in `LIVE_RESULTS.md`.

| Test | Result |
|------|--------|
| `token` | Succeeded with audience `https://ossrdbms-aad.database.windows.net` and object ID `0125c124-d550-446f-b621-8bb7917c2404` |
| `ping` | Succeeded |
| `create-user --database appdb --role readWrite --ensure-database` | Seeded `appdb`, then rejected with `Unsupported value specified for db. Only 'admin' is allowed.` |
| `create-user --database admin --role readWriteAny` | Rejected with `The specified value for the role is invalid: 'readWriteAny'.` |
| `create-user --database admin --role readWriteAnyDatabase` | Rejected with `Roles specified are invalid. Only readAnyDatabase or readWriteAnyDatabase+clusterAdmin are allowed built-in roles.` |

In standard MongoDB, `readWriteAnyDatabase` is a standalone built-in role. Azure DocumentDB's live data-plane response rejected `readWriteAnyDatabase` by itself with the exact error above. The current Azure DocumentDB data-plane documentation shows the read-write secondary user example with both `readWriteAnyDatabase` and `clusterAdmin`; that broad role combination was not applied during validation.

## Notes

- The script requests Azure tokens for `https://ossrdbms-aad.database.windows.net/.default`, matching Microsoft DocumentDB examples.
- The connection uses a PyMongo `OIDC_CALLBACK`; it does not use `mongosh` built-in `ENVIRONMENT:azure` or `ENVIRONMENT:k8s`.
- `verify-write-scope` verifies the currently authenticated identity, not an arbitrary target identity. Use it while authenticated as the user whose permissions you want to test.
