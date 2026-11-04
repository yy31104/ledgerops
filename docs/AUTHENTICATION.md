# Service identity and current permissions

Credentials are random 256-bit opaque strings; the server stores only their SHA-256 digests. Each credential binds a workspace, stable client,
actor, explicit scopes, expiry and restore generation. Principal is derived from these records. Actors or roles supplied in a request cannot grant access.
Store credential files only in protected paths, never in Git, logs, URLs or browser persistence.

## Database identities

An administrator first creates the NOLOGIN groups `ledger_auth_reader` and `ledger_auth_admin`, along with actual login accounts, then applies migration 009.
The application service login inherits `ledger_app`, `ledger_reader` and `ledger_auth_reader`; the identity administrator login
inherits only `ledger_auth_admin`. Both should be NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE/NOREPLICATION
and must not belong to ledger_migrator. The application can read credential digests to authenticate requests but cannot manage credentials or change grants.
The identity administrator can maintain current identities and grants but cannot change financial balances. Migration and restore administrator accounts are kept separately.

These database roles are for trusted service code, not for distribution to end users. The existing Python API accepts a trusted Principal;
the HTTP adapter must always pass the authentication module's guard and cannot let clients select or omit it.
An administrator bypassing management helpers through direct SQL is a separate trust boundary. Cooperative locks do not restrict the database owner.

## Bootstrap, rotation and revocation

Point `LEDGEROPS_AUTH_ADMIN_DSN` at the dedicated identity administrator account. Commands reject ordinary application and superuser logins;
do not put a DSN or password on the command line. The actor must be an actual business owner or operator identity, and the client is a stable calling-application identity.
Omit expected-version on initial creation. Changes to an existing membership or client require its current version, preventing accidental overwrites of another change.

```sh
python -m ledgerops.auth_cli membership --workspace "$WORKSPACE_ID" --actor "$ACTOR_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'initial customer grant' --scope purchase:write
python -m ledgerops.auth_cli client --workspace "$WORKSPACE_ID" --actor "$ACTOR_ID" \
  --client "$CLIENT_ID" --admin-actor "$ADMIN_ACTOR_ID" --reason 'initial client'
python -m ledgerops.auth_cli issue --workspace "$WORKSPACE_ID" --client "$CLIENT_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'credential rotation' --scope purchase:write \
  --ttl-seconds 3600 --output /absolute/private/new-credential.json
python -m ledgerops.auth_cli revoke --workspace "$WORKSPACE_ID" --credential "$CREDENTIAL_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'credential retired'
```

The issue destination must not exist. It is created exclusively with mode 0600 and fsynced. The plaintext token is written only to that file;
stdout returns only the credential ID, expiry and path. Each client may have at most 20 unexpired, unrevoked credentials, with a TTL from 1 second to 30 days.
An interruption may leave an issued but unusable credential. Find it in the identity audit and revoke it; issuance itself performs no financial action.
A failed file write attempts to revoke the new credential. Rotation preserves the client ID, so an existing Idempotency-Key remains in the same namespace and cannot cause another debit merely because the token changed.

A scope must appear in both the credential and current membership. Increasing membership permissions does not expand an old token's scopes.
Disabling a member or client, revoking a credential, expiry or changing the restore generation prevents further use.
The append-only management audit records the operation, administrator database login and actor, reason and before/after state. It includes credential IDs without plaintext tokens.

## Ordering revocation and transactions

Checking authentication only at HTTP entry creates a race: permission can be revoked before the transaction commits. Within its own READ COMMITTED
transaction, a financial entry point first takes the shared workspace barrier, then the shared auth barrier, checks the current credential and permissions,
and only then processes business keys and funds. Purchases, reversals and payouts require the fixed purchase:write, reversal:write and payout:write scopes respectively.
Every full transaction retry authenticates again. Management changes take the same workspace barrier and an exclusive auth barrier, so whichever acquires the lock first determines the ordering.
If the transaction wins, revocation waits for it to commit. If revocation wins, the transaction fails before inserting its business identity.
Expiry uses the database decision clock; a credential expiring at COMMIT does not cancel an operation already admitted. Replaying an original result also requires current permission.

Read-only queries use a separate connection, acquire a session-level shared auth lock, then begin an RR READ ONLY transaction and recheck permissions.
This avoids fixing an old snapshot before waiting for revocation and then reading stale permissions after revocation commits. Revocation waits while the query holds the lock;
completion or an exception unlocks and closes the connection. This context owns the supplied connection; do not return that closed connection to a pool.
Lock waits, statements and transactions are bounded. The HTTP service also needs outer resource limits.
`/v1/me` returns only the current public identity, scopes and expiry, allowing operators without accounts:read to connect.
Account and financial queries still enforce their own fixed scopes; the financial guard does not allow a missing scope.

## Restoring an old backup

Restoring an old database can revive revoked tokens and old approvals. Before opening runtime connections, a restore administrator calls
`invalidate_restored_access`: drain writes, set RESTORE, revoke old credentials, generate a new generation and append an audit record.
Actors, clients and old history remain, and new credentials must be bootstrapped. Idempotent retries with the same restore ID do not revoke credentials created after the first restore action.
Maintenance records and the current freeze reference from an old FROZEN state are preserved. The v8 verifier can represent a freeze retained in RESTORE;
that does not mean the freeze has been resumed. The restore audit also retains the previous mode and freeze reference.

Separate restore and dispatch-disable settings in the running processes are still required. Neither NORMAL in a backup nor a new token establishes that service may reopen.
See HTTP-SERVICE.md for the HTTP service and OPERATOR-ACTIONS.md for controlled two-actor reprocessing and approval invalidation.
Actual pg_dump restore has separate acceptance checks. Financial export v8 declares migration 009; v9 declares 010 and adds approval, execution and resolution relations.
Credentials and identity-management audits are always excluded. Old v1–v8 exports retain their original contracts.
