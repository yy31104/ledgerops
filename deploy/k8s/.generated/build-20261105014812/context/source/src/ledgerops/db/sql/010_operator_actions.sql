-- R5-PR03. One immutable, reviewed terminal LOCAL_BLOCKER reprocessing action.
-- Bootstrap provisions NOLOGIN ledger_operator; its login inherits worker,
-- reader and auth_reader, never auth_admin or migrator. Python is the trusted
-- workflow/authorization boundary; these constraints enforce retained structure.
CREATE TABLE ledger.operator_action (
  workspace_id uuid NOT NULL,
  action_id uuid NOT NULL,
  action_type text NOT NULL CHECK (action_type='REPROCESS_TERMINAL_LOCAL_BLOCKER'),
  payout_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  evidence_run_id uuid NOT NULL,
  intent bytea NOT NULL CHECK (octet_length(intent) BETWEEN 1 AND 4096),
  intent_sha256 bytea GENERATED ALWAYS AS (sha256(intent)) STORED,
  proposal bytea NOT NULL CHECK (octet_length(proposal) BETWEEN 1 AND 1048576),
  proposal_sha256 bytea GENERATED ALWAYS AS (sha256(proposal)) STORED,
  maker_actor_id uuid NOT NULL,
  maker_client_id uuid NOT NULL,
  maker_membership_version bigint NOT NULL CHECK (maker_membership_version>0),
  maker_client_version bigint NOT NULL CHECK (maker_client_version>0),
  auth_generation uuid NOT NULL,
  expires_at timestamptz NOT NULL CHECK (isfinite(expires_at)),
  created_at timestamptz NOT NULL CHECK (isfinite(created_at)),
  PRIMARY KEY(workspace_id,action_id),
  UNIQUE(workspace_id,action_id,proposal_sha256),
  FOREIGN KEY(workspace_id,payout_id,observation_id)
    REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id),
  FOREIGN KEY(workspace_id,evidence_run_id) REFERENCES ledger.recon_run(workspace_id,run_id),
  FOREIGN KEY(workspace_id,maker_client_id) REFERENCES ledger.auth_client(workspace_id,client_id),
  CHECK (expires_at>created_at AND expires_at<=created_at+interval '1 hour')
);
CREATE TABLE ledger.operator_approval (
  workspace_id uuid NOT NULL,
  action_id uuid NOT NULL,
  proposal_sha256 bytea NOT NULL,
  checker_actor_id uuid NOT NULL,
  checker_client_id uuid NOT NULL,
  checker_membership_version bigint NOT NULL CHECK (checker_membership_version>0),
  checker_client_version bigint NOT NULL CHECK (checker_client_version>0),
  approved_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(approved_at)),
  PRIMARY KEY(workspace_id,action_id),
  FOREIGN KEY(workspace_id,action_id,proposal_sha256)
    REFERENCES ledger.operator_action(workspace_id,action_id,proposal_sha256),
  FOREIGN KEY(workspace_id,checker_client_id) REFERENCES ledger.auth_client(workspace_id,client_id)
);
CREATE TABLE ledger.operator_execution (
  workspace_id uuid NOT NULL,
  action_id uuid NOT NULL,
  execution_id uuid NOT NULL,
  proposal_sha256 bytea NOT NULL,
  executor_actor_id uuid NOT NULL,
  executor_client_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  state text NOT NULL CHECK (state IN ('SUCCEEDED','FAILED','CLOSED_ABSENT')),
  journal_id uuid,
  http_status integer NOT NULL CHECK (http_status=200),
  response_body jsonb NOT NULL CHECK (jsonb_typeof(response_body)='object'
    AND octet_length(response_body::text)<=16384),
  executed_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(executed_at)),
  PRIMARY KEY(workspace_id,action_id),
  UNIQUE(workspace_id,action_id,execution_id),
  FOREIGN KEY(workspace_id,action_id) REFERENCES ledger.operator_approval(workspace_id,action_id),
  FOREIGN KEY(workspace_id,action_id,proposal_sha256)
    REFERENCES ledger.operator_action(workspace_id,action_id,proposal_sha256),
  FOREIGN KEY(workspace_id,executor_client_id) REFERENCES ledger.auth_client(workspace_id,client_id),
  CHECK ((state='SUCCEEDED' AND journal_id IS NOT NULL)
    OR (state<>'SUCCEEDED' AND journal_id IS NULL))
);
CREATE TABLE ledger.operator_attempt (
  workspace_id uuid NOT NULL,
  action_id uuid NOT NULL,
  execution_id uuid NOT NULL,
  proposal_sha256 bytea NOT NULL CHECK (octet_length(proposal_sha256)=32),
  executor_actor_id uuid NOT NULL,
  executor_client_id uuid NOT NULL,
  http_status integer NOT NULL CHECK (http_status IN (200,409,422)),
  response_body jsonb NOT NULL CHECK (jsonb_typeof(response_body)='object'
    AND octet_length(response_body::text)<=16384),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(recorded_at)),
  PRIMARY KEY(workspace_id,action_id,execution_id),
  FOREIGN KEY(workspace_id,action_id) REFERENCES ledger.operator_action(workspace_id,action_id),
  FOREIGN KEY(workspace_id,executor_client_id) REFERENCES ledger.auth_client(workspace_id,client_id)
);
CREATE TABLE ledger.payout_incident_resolution (
  workspace_id uuid NOT NULL,
  incident_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  action_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  resolved_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(resolved_at)),
  PRIMARY KEY(workspace_id,incident_id),
  FOREIGN KEY(workspace_id,incident_id) REFERENCES ledger.payout_incident(workspace_id,incident_id),
  FOREIGN KEY(workspace_id,action_id) REFERENCES ledger.operator_execution(workspace_id,action_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(workspace_id,payout_id,observation_id)
    REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id)
);

CREATE FUNCTION ledger.assert_operator_approval() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE maker uuid;
BEGIN
  SELECT maker_actor_id INTO maker FROM ledger.operator_action
    WHERE workspace_id=NEW.workspace_id AND action_id=NEW.action_id;
  IF maker=NEW.checker_actor_id THEN
    RAISE EXCEPTION 'maker cannot approve own action' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER operator_approval_separation BEFORE INSERT ON ledger.operator_approval
  FOR EACH ROW EXECUTE FUNCTION ledger.assert_operator_approval();

CREATE FUNCTION ledger.assert_operator_effect() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE action ledger.operator_action%ROWTYPE; effect ledger.operator_execution%ROWTYPE;
        resolution ledger.payout_resolution%ROWTYPE; incident ledger.payout_incident%ROWTYPE;
        attempt ledger.operator_attempt%ROWTYPE;
BEGIN
  SELECT * INTO action FROM ledger.operator_action
    WHERE workspace_id=NEW.workspace_id AND action_id=NEW.action_id;
  SELECT * INTO effect FROM ledger.operator_execution
    WHERE workspace_id=NEW.workspace_id AND action_id=NEW.action_id;
  IF effect.action_id IS NULL OR effect.observation_id<>action.observation_id THEN
    RAISE EXCEPTION 'action requires its terminal execution' USING ERRCODE='23514';
  END IF;
  SELECT * INTO resolution FROM ledger.payout_resolution
    WHERE workspace_id=NEW.workspace_id AND payout_id=action.payout_id;
  IF resolution.payout_id IS NULL OR
     (resolution.observation_id,resolution.state,resolution.journal_id) IS DISTINCT FROM
     (effect.observation_id,effect.state,effect.journal_id) THEN
    RAISE EXCEPTION 'action execution must match the retained financial resolution' USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='payout_incident_resolution' THEN
    SELECT * INTO incident FROM ledger.payout_incident
      WHERE workspace_id=NEW.workspace_id AND incident_id=NEW.incident_id;
    IF incident.category<>'LOCAL_BLOCKER' OR
      (NEW.payout_id,NEW.observation_id,incident.payout_id,incident.observation_id) IS DISTINCT FROM
      (action.payout_id,action.observation_id,action.payout_id,action.observation_id) THEN
      RAISE EXCEPTION 'only the same source local blocker can be disposed' USING ERRCODE='23514';
    END IF;
  ELSE
    SELECT * INTO attempt FROM ledger.operator_attempt
      WHERE workspace_id=NEW.workspace_id AND action_id=NEW.action_id AND execution_id=effect.execution_id;
    IF attempt.execution_id IS NULL OR attempt.http_status<>effect.http_status
      OR attempt.response_body<>effect.response_body
      OR (attempt.executor_actor_id,attempt.executor_client_id,attempt.proposal_sha256) IS DISTINCT FROM
         (effect.executor_actor_id,effect.executor_client_id,effect.proposal_sha256) THEN
      RAISE EXCEPTION 'execution requires its identical retained attempt' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER operator_execution_effect AFTER INSERT ON ledger.operator_execution
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_operator_effect();
CREATE CONSTRAINT TRIGGER incident_resolution_effect AFTER INSERT ON ledger.payout_incident_resolution
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_operator_effect();

CREATE TRIGGER operator_action_immutable BEFORE UPDATE OR DELETE ON ledger.operator_action
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER operator_approval_immutable BEFORE UPDATE OR DELETE ON ledger.operator_approval
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER operator_execution_immutable BEFORE UPDATE OR DELETE ON ledger.operator_execution
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER operator_attempt_immutable BEFORE UPDATE OR DELETE ON ledger.operator_attempt
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER incident_resolution_immutable BEFORE UPDATE OR DELETE ON ledger.payout_incident_resolution
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

CREATE VIEW ledger.unresolved_payout_incident WITH (security_invoker=true) AS
  SELECT i.* FROM ledger.payout_incident i WHERE NOT EXISTS (
    SELECT 1 FROM ledger.payout_incident_resolution r
      WHERE r.workspace_id=i.workspace_id AND r.incident_id=i.incident_id);

-- Normal workers still cannot restart a blocked intent. Only an operator's
-- terminal settlement may move it to DONE; its resolution is already enforced
-- by assert_payout_admission and its action by the deferred gate below.
CREATE OR REPLACE FUNCTION ledger.protect_dispatch_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF (NEW.workspace_id,NEW.payout_id) IS DISTINCT FROM (OLD.workspace_id,OLD.payout_id)
    OR NOT (
      (OLD.state IN ('READY','IN_FLIGHT','OBSERVING') AND NEW.attempts::numeric=OLD.attempts::numeric+1
        AND NEW.last_attempt_id IS DISTINCT FROM OLD.last_attempt_id AND NEW.state='IN_FLIGHT' AND NEW.next_action='LOOKUP')
      OR (NEW.attempts=OLD.attempts AND NEW.last_attempt_id IS NOT DISTINCT FROM OLD.last_attempt_id
        AND ((OLD.state='IN_FLIGHT' AND NEW.state='OBSERVING')
          OR (OLD.state IN ('READY','IN_FLIGHT','OBSERVING') AND NEW.state IN ('OBSERVING','DONE','BLOCKED'))
          OR (OLD.state='DONE' AND NEW.state='DONE') OR (OLD.state='BLOCKED' AND NEW.state='BLOCKED')
          OR (OLD.state='BLOCKED' AND NEW.state='DONE' AND pg_has_role(current_user,'ledger_operator','USAGE'))))
    ) THEN
    RAISE EXCEPTION 'invalid dispatch transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION ledger.assert_blocked_action() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF OLD.state='BLOCKED' AND NEW.state='DONE' AND NOT EXISTS (
    SELECT 1 FROM ledger.operator_execution e JOIN ledger.operator_action a
      USING(workspace_id,action_id) WHERE a.workspace_id=NEW.workspace_id
      AND a.payout_id=NEW.payout_id) THEN
    RAISE EXCEPTION 'blocked terminal settlement requires an audited action' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER blocked_dispatch_action AFTER UPDATE ON ledger.dispatch_intent
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_blocked_action();

REVOKE ALL ON ledger.operator_action,ledger.operator_approval,ledger.operator_execution,
  ledger.operator_attempt,ledger.payout_incident_resolution,ledger.unresolved_payout_incident FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.assert_operator_approval(),ledger.assert_operator_effect(),
  ledger.assert_blocked_action() FROM PUBLIC;
GRANT USAGE ON SCHEMA ledger TO ledger_operator;
GRANT SELECT,INSERT ON ledger.operator_action,ledger.operator_approval,ledger.operator_execution,
  ledger.operator_attempt,ledger.payout_incident_resolution TO ledger_operator;
-- SELECT FOR UPDATE needs UPDATE privilege; the immutable trigger rejects every mutation.
GRANT UPDATE(action_id) ON ledger.operator_action TO ledger_operator;
GRANT SELECT ON ledger.operator_action,ledger.operator_approval,ledger.operator_execution,
  ledger.operator_attempt,ledger.payout_incident_resolution TO ledger_reader;
GRANT SELECT ON ledger.operator_action,ledger.operator_approval,ledger.operator_execution,
  ledger.payout_incident_resolution TO ledger_maintenance;
GRANT SELECT ON ledger.payout_incident_resolution,ledger.unresolved_payout_incident
  TO ledger_app,ledger_worker,ledger_reader,ledger_maintenance;
