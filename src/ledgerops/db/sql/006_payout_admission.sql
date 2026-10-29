-- R3-PR02: retained payout admission, active holds and conservative transport.
-- Roles (including ledger_worker) must be provisioned by the deployment boundary.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM ledger.account_balance WHERE reserved <> 0) THEN
    RAISE EXCEPTION 'upgrade requires zero legacy reservations; no hold history exists'
      USING ERRCODE = '23514';
  END IF;
END $$;

ALTER TABLE ledger.operation DROP CONSTRAINT operation_kind_check;
ALTER TABLE ledger.operation ADD CONSTRAINT operation_kind_check
  CHECK (kind IN ('OPENING', 'PURCHASE', 'REVERSAL', 'PAYOUT'));
ALTER TABLE ledger.operation DROP CONSTRAINT operation_check1;
ALTER TABLE ledger.operation ADD CONSTRAINT operation_check1 CHECK (
  (admission = 'IN_PROGRESS' AND http_status IS NULL AND response_body IS NULL)
  OR (admission IN ('ACCEPTED','REJECTED') AND http_status IS NOT NULL
    AND response_body IS NOT NULL AND jsonb_typeof(response_body)='object'
    AND ((admission='ACCEPTED' AND http_status=CASE kind WHEN 'PAYOUT' THEN 202 ELSE 201 END)
      OR (admission='REJECTED' AND http_status=422)))
);
ALTER TABLE ledger.request_result DROP CONSTRAINT request_result_route_family_check;
ALTER TABLE ledger.request_result ADD CONSTRAINT request_result_route_family_check
  CHECK (route_family IN ('purchase:v1','reversal:v1','payout:v1'));
ALTER TABLE ledger.request_result DROP CONSTRAINT request_result_check1;
ALTER TABLE ledger.request_result ADD CONSTRAINT request_result_check1 CHECK (
  (http_status IS NULL AND response_body IS NULL)
  OR (http_status IS NOT NULL AND response_body IS NOT NULL
    AND jsonb_typeof(response_body)='object'
    AND (http_status=422 OR http_status=CASE route_family WHEN 'payout:v1' THEN 202 ELSE 201 END))
);

CREATE TABLE ledger.payout (
  workspace_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  merchant_account_id uuid NOT NULL,
  beneficiary_id uuid NOT NULL,
  provider_namespace uuid NOT NULL,
  provider_key text NOT NULL CHECK (octet_length(provider_key) BETWEEN 1 AND 128),
  principal_minor bigint NOT NULL CHECK (principal_minor BETWEEN 1 AND 9000000000000000),
  fee_minor bigint NOT NULL CHECK (fee_minor BETWEEN 0 AND 9000000000000000),
  gross_minor bigint NOT NULL CHECK (gross_minor BETWEEN 1 AND 9000000000000000),
  currency text NOT NULL CHECK (currency='PLN'),
  contract_version integer NOT NULL CHECK (contract_version=1),
  dispatch_deadline timestamptz NOT NULL CHECK (isfinite(dispatch_deadline)),
  provider_request bytea NOT NULL CHECK (octet_length(provider_request) BETWEEN 1 AND 32768),
  provider_request_sha256 bytea NOT NULL CHECK (provider_request_sha256=sha256(provider_request)),
  knowledge text NOT NULL DEFAULT 'NOT_SENT' CHECK (knowledge IN ('NOT_SENT','UNKNOWN')),
  version bigint NOT NULL DEFAULT 1 CHECK (version>=1),
  PRIMARY KEY (workspace_id,operation_id),
  UNIQUE (provider_key),
  FOREIGN KEY (workspace_id,operation_id) REFERENCES ledger.operation(workspace_id,operation_id),
  FOREIGN KEY (workspace_id,merchant_account_id) REFERENCES ledger.account(workspace_id,id),
  CHECK (gross_minor=principal_minor+fee_minor)
);
CREATE TABLE ledger.reservation (
  workspace_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  account_id uuid NOT NULL,
  gross_minor bigint NOT NULL CHECK (gross_minor BETWEEN 1 AND 9000000000000000),
  state text NOT NULL DEFAULT 'HELD' CHECK (state='HELD'),
  version bigint NOT NULL DEFAULT 1 CHECK (version=1),
  PRIMARY KEY (workspace_id,payout_id),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.payout(workspace_id,operation_id),
  FOREIGN KEY (workspace_id,account_id) REFERENCES ledger.account(workspace_id,id)
);
CREATE INDEX reservation_account_idx ON ledger.reservation(workspace_id,account_id);
CREATE TABLE ledger.reservation_event (
  workspace_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  event_no bigint NOT NULL CHECK (event_no=1),
  state text NOT NULL CHECK (state='HELD'),
  observation_id uuid CHECK (observation_id IS NULL),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id,payout_id,event_no),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.reservation(workspace_id,payout_id)
);
CREATE TABLE ledger.dispatch_intent (
  workspace_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'READY' CHECK (state IN ('READY','IN_FLIGHT','OBSERVING')),
  next_action text NOT NULL DEFAULT 'PUT' CHECK (next_action IN ('PUT','LOOKUP','CLOSE')),
  next_due_at timestamptz NOT NULL CHECK (isfinite(next_due_at)),
  attempts bigint NOT NULL DEFAULT 0 CHECK (attempts>=0),
  last_attempt_id uuid,
  PRIMARY KEY (workspace_id,payout_id),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.payout(workspace_id,operation_id),
  CHECK ((attempts=0 AND last_attempt_id IS NULL AND state='READY' AND next_action='PUT')
    OR (attempts>0 AND last_attempt_id IS NOT NULL AND state IN ('IN_FLIGHT','OBSERVING')))
);
CREATE INDEX dispatch_due_idx ON ledger.dispatch_intent(next_due_at,workspace_id,payout_id);
CREATE TABLE ledger.transport_attempt (
  workspace_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('PUT','LOOKUP','CLOSE')),
  started_at timestamptz NOT NULL CHECK (isfinite(started_at)),
  finished_at timestamptz CHECK (isfinite(finished_at)),
  outcome text NOT NULL DEFAULT 'MAY_HAVE_SENT'
    CHECK (outcome IN ('MAY_HAVE_SENT','RESPONSE','TIMEOUT','CONNECTION_ERROR','TRANSPORT_ERROR','INVALID_RESPONSE')),
  http_status integer CHECK (http_status BETWEEN 100 AND 599),
  response_payload bytea CHECK (octet_length(response_payload)<=65536),
  PRIMARY KEY (workspace_id,attempt_id),
  UNIQUE (workspace_id,payout_id,attempt_id),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.dispatch_intent(workspace_id,payout_id),
  CHECK ((outcome='MAY_HAVE_SENT' AND finished_at IS NULL AND http_status IS NULL AND response_payload IS NULL)
    OR (outcome<>'MAY_HAVE_SENT' AND finished_at IS NOT NULL
        AND (outcome NOT IN ('RESPONSE','INVALID_RESPONSE') OR (http_status IS NOT NULL AND response_payload IS NOT NULL))))
);
ALTER TABLE ledger.dispatch_intent ADD CONSTRAINT dispatch_last_attempt_fk
  FOREIGN KEY (workspace_id,payout_id,last_attempt_id)
  REFERENCES ledger.transport_attempt(workspace_id,payout_id,attempt_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION ledger.protect_payout_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF (to_jsonb(NEW)-'knowledge'-'version') IS DISTINCT FROM (to_jsonb(OLD)-'knowledge'-'version')
     OR NEW.version::numeric<>OLD.version::numeric+1 OR NEW.knowledge<>'UNKNOWN' THEN
    RAISE EXCEPTION 'invalid payout transport transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION ledger.protect_dispatch_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF (NEW.workspace_id,NEW.payout_id) IS DISTINCT FROM (OLD.workspace_id,OLD.payout_id)
     OR NOT (
       (NEW.attempts::numeric=OLD.attempts::numeric+1 AND NEW.last_attempt_id IS DISTINCT FROM OLD.last_attempt_id
         AND NEW.state='IN_FLIGHT' AND NEW.next_action='LOOKUP')
       OR (NEW.attempts=OLD.attempts AND NEW.last_attempt_id=OLD.last_attempt_id
         AND OLD.state='IN_FLIGHT' AND NEW.state='OBSERVING')
     ) THEN
    RAISE EXCEPTION 'invalid dispatch transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION ledger.protect_transport_finish() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF OLD.outcome<>'MAY_HAVE_SENT' OR NEW.outcome='MAY_HAVE_SENT'
     OR (NEW.workspace_id,NEW.attempt_id,NEW.payout_id,NEW.action,NEW.started_at)
       IS DISTINCT FROM (OLD.workspace_id,OLD.attempt_id,OLD.payout_id,OLD.action,OLD.started_at) THEN
    RAISE EXCEPTION 'attempt allows only one completion' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION ledger.assert_payout_admission() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE id uuid; op ledger.operation%ROWTYPE; p ledger.payout%ROWTYPE;
        r ledger.reservation%ROWTYPE; has_payout boolean;
BEGIN
  IF TG_TABLE_NAME IN ('reservation','reservation_event','dispatch_intent') THEN id:=NEW.payout_id;
  ELSE id:=NEW.operation_id; END IF;
  SELECT * INTO op FROM ledger.operation WHERE workspace_id=NEW.workspace_id AND operation_id=id;
  IF NOT FOUND THEN RAISE EXCEPTION 'payout operation is missing' USING ERRCODE='23514'; END IF;
  IF op.kind<>'PAYOUT' THEN
    IF TG_TABLE_NAME IN ('payout','reservation','reservation_event','dispatch_intent') THEN
      RAISE EXCEPTION 'payout requires PAYOUT operation' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO p FROM ledger.payout WHERE workspace_id=NEW.workspace_id AND operation_id=id;
  has_payout:=FOUND;
  IF op.admission='REJECTED' THEN
    IF has_payout OR EXISTS(SELECT 1 FROM ledger.journal WHERE workspace_id=NEW.workspace_id AND operation_id=id) THEN
      RAISE EXCEPTION 'rejected payout has financial artifacts' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
  END IF;
  IF op.admission<>'ACCEPTED' OR op.http_status<>202 OR NOT has_payout THEN
    RAISE EXCEPTION 'payout admission is incomplete' USING ERRCODE='23514';
  END IF;
  SELECT * INTO r FROM ledger.reservation WHERE workspace_id=NEW.workspace_id AND payout_id=id;
  IF NOT FOUND OR r.account_id<>p.merchant_account_id OR r.gross_minor<>p.gross_minor
    OR NOT EXISTS (SELECT 1 FROM ledger.reservation_event WHERE workspace_id=NEW.workspace_id AND payout_id=id AND event_no=1)
    OR NOT EXISTS (SELECT 1 FROM ledger.dispatch_intent WHERE workspace_id=NEW.workspace_id AND payout_id=id)
    OR EXISTS (SELECT 1 FROM ledger.journal WHERE workspace_id=NEW.workspace_id AND operation_id=id) THEN
    RAISE EXCEPTION 'payout requires one hold and intent, without a journal' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER payout_transition BEFORE UPDATE ON ledger.payout
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_payout_transition();
CREATE TRIGGER payout_no_delete BEFORE DELETE ON ledger.payout
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER reservation_immutable BEFORE UPDATE OR DELETE ON ledger.reservation
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER reservation_event_immutable BEFORE UPDATE OR DELETE ON ledger.reservation_event
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER dispatch_transition BEFORE UPDATE ON ledger.dispatch_intent
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_dispatch_transition();
CREATE TRIGGER dispatch_no_delete BEFORE DELETE ON ledger.dispatch_intent
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER transport_transition BEFORE UPDATE ON ledger.transport_attempt
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_transport_finish();
CREATE TRIGGER transport_no_delete BEFORE DELETE ON ledger.transport_attempt
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE CONSTRAINT TRIGGER payout_final_on_operation AFTER INSERT OR UPDATE ON ledger.operation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_payout AFTER INSERT OR UPDATE ON ledger.payout
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_reservation AFTER INSERT ON ledger.reservation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_event AFTER INSERT ON ledger.reservation_event
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_intent AFTER INSERT OR UPDATE ON ledger.dispatch_intent
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_journal AFTER INSERT ON ledger.journal
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();

-- Audit format1 retains the old no-reserve contract; format2 folds hold history.
ALTER TABLE ledger.projection_repair ADD COLUMN fold_version integer NOT NULL DEFAULT 1
  CHECK (fold_version IN (1,2));
ALTER TABLE ledger.projection_repair_row DROP CONSTRAINT projection_repair_row_after_reserved_check;
ALTER TABLE ledger.projection_repair_row DROP CONSTRAINT projection_repair_row_check1;
ALTER TABLE ledger.projection_repair_row ADD CONSTRAINT projection_repair_row_check1 CHECK (
  (before_debits IS NULL AND before_credits IS NULL AND before_reserved IS NULL
    AND before_booked IS NULL AND before_version IS NULL AND after_version=1)
  OR (before_debits IS NOT NULL AND before_credits IS NOT NULL AND before_reserved IS NOT NULL
    AND before_booked IS NOT NULL AND before_version IS NOT NULL AND before_version>=0
    AND before_booked=CASE normal_side WHEN 'D' THEN before_debits-before_credits ELSE before_credits-before_debits END
    AND before_booked>=before_reserved AND after_version::numeric=before_version::numeric+1)
);
CREATE OR REPLACE FUNCTION ledger.assert_repair_matches_declaration() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE declared_accounts integer; actual_accounts bigint; fold integer;
BEGIN
  SELECT account_count,fold_version INTO declared_accounts,fold FROM ledger.projection_repair
    WHERE workspace_id=NEW.workspace_id AND repair_id=NEW.repair_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'repair header is missing' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO actual_accounts FROM ledger.projection_repair_row
    WHERE workspace_id=NEW.workspace_id AND repair_id=NEW.repair_id;
  IF actual_accounts<>declared_accounts OR (fold=1 AND EXISTS (
    SELECT 1 FROM ledger.projection_repair_row WHERE workspace_id=NEW.workspace_id AND repair_id=NEW.repair_id
      AND (after_reserved<>0 OR coalesce(before_reserved,0)<>0))) THEN
    RAISE EXCEPTION 'repair does not match immutable declaration or fold version' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;

REVOKE ALL ON ledger.payout,ledger.reservation,ledger.reservation_event,
  ledger.dispatch_intent,ledger.transport_attempt FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.protect_payout_transition(),ledger.protect_dispatch_transition(),
  ledger.protect_transport_finish(),ledger.assert_payout_admission() FROM PUBLIC;
GRANT SELECT ON ledger.payout,ledger.reservation,ledger.reservation_event,
  ledger.dispatch_intent,ledger.transport_attempt TO ledger_app,ledger_reader,ledger_maintenance;
GRANT INSERT ON ledger.payout,ledger.reservation,ledger.reservation_event,ledger.dispatch_intent TO ledger_app;
GRANT INSERT (reserved), UPDATE (reserved) ON ledger.account_balance TO ledger_maintenance;
GRANT USAGE ON SCHEMA ledger TO ledger_worker;
GRANT SELECT ON ledger.workspace,ledger.account,ledger.account_balance,ledger.operation,ledger.journal,
  ledger.payout,ledger.reservation,ledger.reservation_event,ledger.dispatch_intent,ledger.transport_attempt
  TO ledger_worker;
-- SELECT FOR UPDATE needs UPDATE privilege; terminal operation guard still forbids mutation.
GRANT UPDATE (admission) ON ledger.operation TO ledger_worker;
GRANT UPDATE (knowledge,version) ON ledger.payout TO ledger_worker;
GRANT UPDATE (state,next_action,next_due_at,attempts,last_attempt_id) ON ledger.dispatch_intent TO ledger_worker;
GRANT INSERT ON ledger.transport_attempt TO ledger_worker;
GRANT UPDATE (finished_at,outcome,http_status,response_payload) ON ledger.transport_attempt TO ledger_worker;
