-- R5-PR01. Opaque credentials, current workspace grants and immutable management audit.
-- Bootstrap must provision NOLOGIN ledger_auth_reader and ledger_auth_admin first.
-- Financial/runtime roles do not inherit credential-management privileges.
CREATE FUNCTION ledger.auth_scopes_valid(value text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
  SELECT value IS NOT NULL AND cardinality(value) BETWEEN 0 AND 13
    AND array_position(value,NULL) IS NULL
    AND value <@ ARRAY['accounts:read','approval:approve','approval:execute','approval:propose',
      'evidence:read','maintenance:freeze','maintenance:preview','maintenance:resume',
      'operations:read','payout:read','payout:write','purchase:write','reversal:write']::text[]
    AND value=ARRAY(SELECT DISTINCT scope COLLATE "C" FROM unnest(value) scope ORDER BY 1)
$$;

CREATE TABLE ledger.auth_workspace_state (
  workspace_id uuid PRIMARY KEY REFERENCES ledger.workspace(id),
  generation uuid NOT NULL,
  version bigint NOT NULL DEFAULT 1 CHECK (version>0)
);
CREATE TABLE ledger.auth_membership (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  actor_id uuid NOT NULL,
  scopes text[] NOT NULL CHECK (ledger.auth_scopes_valid(scopes)),
  enabled boolean NOT NULL DEFAULT true,
  version bigint NOT NULL DEFAULT 1 CHECK (version>0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
  PRIMARY KEY(workspace_id,actor_id)
);
CREATE TABLE ledger.auth_client (
  workspace_id uuid NOT NULL,
  client_id uuid NOT NULL,
  actor_id uuid NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  version bigint NOT NULL DEFAULT 1 CHECK (version>0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
  PRIMARY KEY(workspace_id,client_id),
  FOREIGN KEY(workspace_id,actor_id) REFERENCES ledger.auth_membership(workspace_id,actor_id)
);
CREATE TABLE ledger.auth_credential (
  workspace_id uuid NOT NULL,
  credential_id uuid NOT NULL,
  client_id uuid NOT NULL,
  generation uuid NOT NULL,
  token_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(token_sha256)=32),
  scopes text[] NOT NULL CHECK (ledger.auth_scopes_valid(scopes)),
  issued_at timestamptz NOT NULL CHECK (isfinite(issued_at)),
  expires_at timestamptz NOT NULL CHECK (isfinite(expires_at) AND expires_at>issued_at),
  revoked_at timestamptz CHECK (isfinite(revoked_at)),
  version bigint NOT NULL DEFAULT 1 CHECK (version>0),
  PRIMARY KEY(workspace_id,credential_id),
  FOREIGN KEY(workspace_id,client_id) REFERENCES ledger.auth_client(workspace_id,client_id)
);
CREATE INDEX auth_credential_client_idx ON ledger.auth_credential(workspace_id,client_id);
CREATE TABLE ledger.auth_audit (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  audit_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('WORKSPACE_BOOTSTRAPPED','MEMBERSHIP_CREATED','MEMBERSHIP_CHANGED',
    'CLIENT_CREATED','CLIENT_CHANGED','CREDENTIAL_ISSUED','CREDENTIAL_REVOKED')),
  admin_actor_id uuid NOT NULL,
  admin_database_role name NOT NULL DEFAULT session_user,
  subject_actor_id uuid,
  client_id uuid,
  credential_id uuid,
  reason text NOT NULL CHECK (octet_length(reason) BETWEEN 1 AND 256),
  before_state jsonb CHECK (jsonb_typeof(before_state)='object' AND octet_length(before_state::text)<=8192),
  after_state jsonb NOT NULL CHECK (jsonb_typeof(after_state)='object' AND octet_length(after_state::text)<=8192),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(workspace_id,audit_id),
  FOREIGN KEY(workspace_id,subject_actor_id) REFERENCES ledger.auth_membership(workspace_id,actor_id),
  FOREIGN KEY(workspace_id,client_id) REFERENCES ledger.auth_client(workspace_id,client_id),
  FOREIGN KEY(workspace_id,credential_id) REFERENCES ledger.auth_credential(workspace_id,credential_id)
);
CREATE TABLE ledger.auth_restore_run (
  restore_id uuid PRIMARY KEY,
  backup_id uuid NOT NULL,
  workspace_ids uuid[] NOT NULL,
  admin_actor_id uuid NOT NULL,
  admin_database_role name NOT NULL DEFAULT session_user,
  reason text NOT NULL CHECK (octet_length(reason) BETWEEN 1 AND 256),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (cardinality(workspace_ids)<=10000 AND array_position(workspace_ids,NULL) IS NULL)
);
CREATE TABLE ledger.auth_restore_workspace (
  restore_id uuid NOT NULL REFERENCES ledger.auth_restore_run(restore_id),
  workspace_id uuid NOT NULL REFERENCES ledger.auth_workspace_state(workspace_id),
  previous_generation uuid NOT NULL,
  generation uuid NOT NULL,
  previous_mode text NOT NULL CHECK (previous_mode IN ('NORMAL','FROZEN','RESTORE')),
  previous_freeze_id uuid,
  previous_version bigint NOT NULL,
  version bigint NOT NULL,
  revoked_count bigint NOT NULL CHECK (revoked_count>=0),
  PRIMARY KEY(restore_id,workspace_id),
  UNIQUE(workspace_id,generation),
  CHECK (generation<>previous_generation AND version=previous_version+1)
);

CREATE FUNCTION ledger.auth_identity_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retained auth identity' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME='auth_membership' THEN
    IF (NEW.workspace_id,NEW.actor_id,NEW.created_at) IS DISTINCT FROM
       (OLD.workspace_id,OLD.actor_id,OLD.created_at) OR NEW.version<>OLD.version+1 THEN
      RAISE EXCEPTION 'invalid membership transition' USING ERRCODE='23514';
    END IF;
  ELSIF TG_TABLE_NAME='auth_client' THEN
    IF (NEW.workspace_id,NEW.client_id,NEW.actor_id,NEW.created_at) IS DISTINCT FROM
       (OLD.workspace_id,OLD.client_id,OLD.actor_id,OLD.created_at) OR NEW.version<>OLD.version+1 THEN
      RAISE EXCEPTION 'immutable client assignment or invalid version' USING ERRCODE='23514';
    END IF;
  ELSE
    IF (NEW.workspace_id,NEW.credential_id,NEW.client_id,NEW.generation,NEW.token_sha256,NEW.scopes,NEW.issued_at,NEW.expires_at)
       IS DISTINCT FROM
       (OLD.workspace_id,OLD.credential_id,OLD.client_id,OLD.generation,OLD.token_sha256,OLD.scopes,OLD.issued_at,OLD.expires_at)
       OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.version<>OLD.version+1 THEN
      RAISE EXCEPTION 'immutable credential or invalid revocation' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER auth_membership_identity BEFORE UPDATE OR DELETE ON ledger.auth_membership
FOR EACH ROW EXECUTE FUNCTION ledger.auth_identity_guard();
CREATE TRIGGER auth_client_identity BEFORE UPDATE OR DELETE ON ledger.auth_client
FOR EACH ROW EXECUTE FUNCTION ledger.auth_identity_guard();
CREATE TRIGGER auth_credential_identity BEFORE UPDATE OR DELETE ON ledger.auth_credential
FOR EACH ROW EXECUTE FUNCTION ledger.auth_identity_guard();
CREATE FUNCTION ledger.auth_audit_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'immutable auth management audit' USING ERRCODE='23514';
END $$;
CREATE TRIGGER auth_audit_immutable BEFORE UPDATE OR DELETE ON ledger.auth_audit
FOR EACH ROW EXECUTE FUNCTION ledger.auth_audit_immutable();
CREATE TRIGGER auth_restore_run_immutable BEFORE UPDATE OR DELETE ON ledger.auth_restore_run
FOR EACH ROW EXECUTE FUNCTION ledger.auth_audit_immutable();
CREATE TRIGGER auth_restore_workspace_immutable BEFORE UPDATE OR DELETE ON ledger.auth_restore_workspace
FOR EACH ROW EXECUTE FUNCTION ledger.auth_audit_immutable();

REVOKE ALL ON ledger.auth_membership,ledger.auth_client,ledger.auth_credential,ledger.auth_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.auth_scopes_valid(text[]),ledger.auth_identity_guard(),ledger.auth_audit_immutable() FROM PUBLIC;
GRANT USAGE ON SCHEMA ledger TO ledger_auth_reader,ledger_auth_admin;
GRANT SELECT ON ledger.auth_workspace_state,ledger.auth_membership,ledger.auth_client,ledger.auth_credential TO ledger_auth_reader;
GRANT SELECT,INSERT ON ledger.auth_membership,ledger.auth_client,ledger.auth_credential,ledger.auth_audit TO ledger_auth_admin;
GRANT SELECT,INSERT ON ledger.auth_workspace_state,ledger.auth_restore_run,ledger.auth_restore_workspace TO ledger_auth_admin;
GRANT SELECT(id) ON ledger.workspace TO ledger_auth_admin;
GRANT UPDATE(generation,version) ON ledger.auth_workspace_state TO ledger_auth_admin;
GRANT UPDATE(scopes,enabled,version) ON ledger.auth_membership TO ledger_auth_admin;
GRANT UPDATE(enabled,version) ON ledger.auth_client TO ledger_auth_admin;
GRANT UPDATE(revoked_at,version) ON ledger.auth_credential TO ledger_auth_admin;
GRANT EXECUTE ON FUNCTION ledger.auth_scopes_valid(text[]) TO ledger_auth_admin;
