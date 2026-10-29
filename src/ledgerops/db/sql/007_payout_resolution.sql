-- R3-PR03. Authenticated source evidence and one local terminal financial effect.
ALTER TABLE ledger.payout ADD COLUMN source_sequence bigint NOT NULL DEFAULT 0 CHECK (source_sequence>=0);
ALTER TABLE ledger.payout ADD UNIQUE (workspace_id,operation_id,provider_namespace);
ALTER TABLE ledger.payout DROP CONSTRAINT payout_knowledge_check;
ALTER TABLE ledger.payout ADD CONSTRAINT payout_knowledge_check
  CHECK (knowledge IN ('NOT_SENT','UNKNOWN','ACCEPTED','SUCCEEDED','FAILED','CLOSED_ABSENT'));
ALTER TABLE ledger.reservation DROP CONSTRAINT reservation_state_check;
ALTER TABLE ledger.reservation ADD CONSTRAINT reservation_state_check
  CHECK (state IN ('HELD','CONSUMED','RELEASED'));
ALTER TABLE ledger.reservation DROP CONSTRAINT reservation_version_check;
ALTER TABLE ledger.reservation ADD CONSTRAINT reservation_version_check
  CHECK ((state='HELD' AND version=1) OR (state IN ('CONSUMED','RELEASED') AND version=2));
ALTER TABLE ledger.reservation_event DROP CONSTRAINT reservation_event_event_no_check;
ALTER TABLE ledger.reservation_event DROP CONSTRAINT reservation_event_state_check;
ALTER TABLE ledger.reservation_event DROP CONSTRAINT reservation_event_observation_id_check;
ALTER TABLE ledger.reservation_event ADD CONSTRAINT reservation_event_lifecycle_check CHECK (
  (event_no=1 AND state='HELD' AND observation_id IS NULL)
  OR (event_no=2 AND state IN ('CONSUMED','RELEASED') AND observation_id IS NOT NULL)
);
ALTER TABLE ledger.dispatch_intent DROP CONSTRAINT dispatch_intent_state_check;
ALTER TABLE ledger.dispatch_intent ADD CONSTRAINT dispatch_intent_state_check
  CHECK (state IN ('READY','IN_FLIGHT','OBSERVING','DONE','BLOCKED'));
ALTER TABLE ledger.dispatch_intent DROP CONSTRAINT dispatch_intent_check;
ALTER TABLE ledger.dispatch_intent ADD CONSTRAINT dispatch_intent_check CHECK (
  ((attempts=0 AND last_attempt_id IS NULL) OR (attempts>0 AND last_attempt_id IS NOT NULL))
  AND (state<>'READY' OR (attempts=0 AND next_action='PUT'))
);

CREATE TABLE ledger.provider_observation (
  observation_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  provider_namespace uuid NOT NULL,
  event_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('lookup','execution')),
  state text NOT NULL CHECK (state IN ('NOT_FOUND','ACCEPTED','SUCCEEDED','FAILED','CLOSED_ABSENT')),
  source_sequence bigint NOT NULL CHECK (source_sequence>=0),
  key_id text NOT NULL CHECK (key_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  signed_payload text NOT NULL CHECK (octet_length(signed_payload) BETWEEN 1 AND 65536),
  signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{64}$'),
  envelope bytea NOT NULL CHECK (octet_length(envelope) BETWEEN 1 AND 65536),
  envelope_sha256 bytea NOT NULL CHECK (envelope_sha256=sha256(envelope)),
  received_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id,payout_id,observation_id),
  FOREIGN KEY (workspace_id,payout_id,provider_namespace)
    REFERENCES ledger.payout(workspace_id,operation_id,provider_namespace),
  CHECK ((kind='lookup' AND state='NOT_FOUND') OR (kind='execution' AND state<>'NOT_FOUND' AND source_sequence>0))
);
CREATE INDEX observation_identity_idx ON ledger.provider_observation(provider_namespace,event_id);
CREATE INDEX observation_sequence_idx ON ledger.provider_observation(provider_namespace,source_sequence) WHERE kind='execution';
CREATE INDEX observation_payout_idx ON ledger.provider_observation(workspace_id,payout_id);

CREATE TABLE ledger.observation_receipt (
  workspace_id uuid NOT NULL,
  receipt_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('PUT','LOOKUP','CLOSE','CALLBACK','HISTORY')),
  transport_attempt_id uuid,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id,receipt_id),
  UNIQUE (observation_id,channel),
  FOREIGN KEY (workspace_id,payout_id,observation_id)
    REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id),
  FOREIGN KEY (workspace_id,payout_id,transport_attempt_id)
    REFERENCES ledger.transport_attempt(workspace_id,payout_id,attempt_id)
);
CREATE TABLE ledger.observation_quarantine (
  workspace_id uuid NOT NULL,
  quarantine_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('PUT','LOOKUP','CLOSE','CALLBACK','HISTORY')),
  reason text NOT NULL CHECK (reason='INVALID_OBSERVATION'),
  raw_payload bytea NOT NULL CHECK (octet_length(raw_payload)<=65536),
  raw_sha256 bytea NOT NULL CHECK (raw_sha256=sha256(raw_payload)),
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id,quarantine_id),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.payout(workspace_id,operation_id)
);
CREATE INDEX quarantine_payout_idx ON ledger.observation_quarantine(workspace_id,payout_id);
CREATE INDEX quarantine_digest_idx ON ledger.observation_quarantine(workspace_id,payout_id,raw_sha256);
CREATE TABLE ledger.payout_incident (
  workspace_id uuid NOT NULL,
  incident_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  category text NOT NULL CHECK (category IN ('PROVIDER_CONTRADICTION','LOCAL_BLOCKER','EVIDENCE_LIMIT')),
  code text NOT NULL CHECK (code IN ('EVENT_ID_REUSED','SEQUENCE_REUSED','TERMINAL_CONTRADICTION',
    'NONTERMINAL_AFTER_FINAL','PROJECTION_MISSING','PROJECTION_MISMATCH','CAPACITY_EXCEEDED','HOLD_PROJECTION_MISMATCH',
    'EVIDENCE_CAPACITY')),
  observation_id uuid,
  related_observation_id uuid REFERENCES ledger.provider_observation(observation_id),
  related_payload_sha256 bytea CHECK (octet_length(related_payload_sha256)=32),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id,incident_id),
  FOREIGN KEY (workspace_id,payout_id) REFERENCES ledger.payout(workspace_id,operation_id),
  FOREIGN KEY (workspace_id,payout_id,observation_id)
    REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id),
  CHECK ((category='EVIDENCE_LIMIT' AND code='EVIDENCE_CAPACITY' AND observation_id IS NULL
      AND related_observation_id IS NULL AND related_payload_sha256 IS NOT NULL)
    OR (category='LOCAL_BLOCKER' AND code IN ('PROJECTION_MISSING','PROJECTION_MISMATCH','CAPACITY_EXCEEDED','HOLD_PROJECTION_MISMATCH')
      AND observation_id IS NOT NULL AND related_observation_id IS NULL)
    OR (category='PROVIDER_CONTRADICTION' AND code IN ('EVENT_ID_REUSED','SEQUENCE_REUSED',
      'TERMINAL_CONTRADICTION','NONTERMINAL_AFTER_FINAL') AND observation_id IS NOT NULL
      AND related_observation_id IS NOT NULL AND related_payload_sha256 IS NOT NULL))
);
CREATE INDEX incident_payout_idx ON ledger.payout_incident(workspace_id,payout_id);
CREATE INDEX incident_related_idx ON ledger.payout_incident(related_observation_id);
CREATE TABLE ledger.payout_resolution (
  workspace_id uuid NOT NULL,
  payout_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  state text NOT NULL CHECK (state IN ('SUCCEEDED','FAILED','CLOSED_ABSENT')),
  journal_id uuid,
  resolved_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id,payout_id),
  FOREIGN KEY (workspace_id,payout_id,observation_id)
    REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id),
  FOREIGN KEY (workspace_id,journal_id) REFERENCES ledger.journal(workspace_id,journal_id),
  CHECK ((state='SUCCEEDED' AND journal_id IS NOT NULL) OR (state<>'SUCCEEDED' AND journal_id IS NULL))
);
ALTER TABLE ledger.reservation_event ADD CONSTRAINT reservation_observation_fk
  FOREIGN KEY (workspace_id,payout_id,observation_id)
  REFERENCES ledger.provider_observation(workspace_id,payout_id,observation_id);

-- Cross-workspace collisions affect later decisions without writing another
-- workspace under the wrong barrier. Expose only the local reference and the
-- conflicting envelope digest, never another workspace's payload or identity.
CREATE VIEW ledger.related_payout_incident WITH (security_invoker=true) AS
SELECT o.workspace_id,i.incident_id,o.payout_id,o.observation_id,i.code,
       incoming.envelope_sha256 AS related_payload_sha256,i.created_at
FROM ledger.payout_incident i
JOIN ledger.provider_observation o ON o.observation_id=i.related_observation_id
JOIN ledger.provider_observation incoming ON incoming.observation_id=i.observation_id
WHERE i.workspace_id<>o.workspace_id;

CREATE OR REPLACE FUNCTION ledger.protect_payout_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF (to_jsonb(NEW)-'knowledge'-'version'-'source_sequence') IS DISTINCT FROM
     (to_jsonb(OLD)-'knowledge'-'version'-'source_sequence')
     OR NEW.version::numeric<>OLD.version::numeric+1 OR NEW.source_sequence<OLD.source_sequence
     OR NEW.knowledge='NOT_SENT'
     OR (OLD.knowledge IN ('SUCCEEDED','FAILED','CLOSED_ABSENT') AND NEW.knowledge<>OLD.knowledge)
     OR (OLD.knowledge='ACCEPTED' AND NEW.knowledge='UNKNOWN') THEN
    RAISE EXCEPTION 'invalid payout knowledge transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
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
          OR (OLD.state='DONE' AND NEW.state='DONE') OR (OLD.state='BLOCKED' AND NEW.state='BLOCKED')))
    ) THEN
    RAISE EXCEPTION 'invalid dispatch transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION ledger.protect_reservation_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF OLD.state<>'HELD' OR NEW.state NOT IN ('CONSUMED','RELEASED') OR NEW.version<>2
    OR (NEW.workspace_id,NEW.payout_id,NEW.account_id,NEW.gross_minor)
      IS DISTINCT FROM (OLD.workspace_id,OLD.payout_id,OLD.account_id,OLD.gross_minor) THEN
    RAISE EXCEPTION 'hold permits exactly one terminal transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER reservation_immutable ON ledger.reservation;
CREATE TRIGGER reservation_transition BEFORE UPDATE ON ledger.reservation
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_reservation_transition();
CREATE TRIGGER reservation_no_delete BEFORE DELETE ON ledger.reservation
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

CREATE OR REPLACE FUNCTION ledger.assert_payout_admission() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE id uuid; op ledger.operation%ROWTYPE; p ledger.payout%ROWTYPE;
        r ledger.reservation%ROWTYPE; res ledger.payout_resolution%ROWTYPE;
        obs ledger.provider_observation%ROWTYPE; j ledger.journal%ROWTYPE;
        intent_state text; event_count bigint; has_payout boolean; has_resolution boolean; has_journal boolean;
BEGIN
  IF TG_TABLE_NAME IN ('reservation','reservation_event','dispatch_intent','payout_resolution') THEN id:=NEW.payout_id;
  ELSE id:=NEW.operation_id; END IF;
  SELECT * INTO op FROM ledger.operation WHERE workspace_id=NEW.workspace_id AND operation_id=id;
  IF NOT FOUND THEN RAISE EXCEPTION 'payout operation is missing' USING ERRCODE='23514'; END IF;
  IF op.kind<>'PAYOUT' THEN
    IF TG_TABLE_NAME IN ('payout','reservation','reservation_event','dispatch_intent','payout_resolution') THEN
      RAISE EXCEPTION 'payout requires PAYOUT operation' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO p FROM ledger.payout WHERE workspace_id=NEW.workspace_id AND operation_id=id;
  has_payout:=FOUND;
  SELECT * INTO j FROM ledger.journal WHERE workspace_id=NEW.workspace_id AND operation_id=id;
  has_journal:=FOUND;
  IF op.admission='REJECTED' THEN
    IF has_payout OR has_journal THEN RAISE EXCEPTION 'rejected payout has financial artifacts' USING ERRCODE='23514'; END IF;
    RETURN NULL;
  END IF;
  IF op.admission<>'ACCEPTED' OR op.http_status<>202 OR NOT has_payout THEN
    RAISE EXCEPTION 'payout admission is incomplete' USING ERRCODE='23514';
  END IF;
  SELECT * INTO r FROM ledger.reservation WHERE workspace_id=NEW.workspace_id AND payout_id=id;
  IF NOT FOUND OR r.account_id<>p.merchant_account_id OR r.gross_minor<>p.gross_minor THEN
    RAISE EXCEPTION 'payout hold instruction mismatch' USING ERRCODE='23514';
  END IF;
  SELECT state INTO intent_state FROM ledger.dispatch_intent WHERE workspace_id=NEW.workspace_id AND payout_id=id;
  IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM ledger.reservation_event
      WHERE workspace_id=NEW.workspace_id AND payout_id=id AND event_no=1 AND state='HELD') THEN
    RAISE EXCEPTION 'payout requires its hold history and intent' USING ERRCODE='23514';
  END IF;
  SELECT count(*) INTO event_count FROM ledger.reservation_event WHERE workspace_id=NEW.workspace_id AND payout_id=id;
  SELECT * INTO res FROM ledger.payout_resolution WHERE workspace_id=NEW.workspace_id AND payout_id=id;
  has_resolution:=FOUND;
  IF NOT has_resolution THEN
    IF r.state<>'HELD' OR event_count<>1 OR has_journal OR intent_state='DONE'
      OR p.knowledge NOT IN ('NOT_SENT','UNKNOWN','ACCEPTED') THEN
      RAISE EXCEPTION 'unresolved payout cannot have a terminal financial effect' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO obs FROM ledger.provider_observation WHERE observation_id=res.observation_id;
  IF NOT FOUND OR obs.kind<>'execution' OR obs.state<>res.state OR obs.payout_id<>id
    OR obs.workspace_id<>NEW.workspace_id OR p.knowledge<>res.state OR intent_state<>'DONE'
    OR event_count<>2 OR NOT EXISTS (SELECT 1 FROM ledger.reservation_event
      WHERE workspace_id=NEW.workspace_id AND payout_id=id AND event_no=2
        AND observation_id=res.observation_id AND state=r.state) THEN
    RAISE EXCEPTION 'terminal payout requires matching observation and hold event' USING ERRCODE='23514';
  END IF;
  IF res.state='SUCCEEDED' THEN
    IF r.state<>'CONSUMED' OR NOT has_journal OR j.journal_id<>res.journal_id
      OR j.kind<>'PAYOUT' OR j.line_count<>2 OR j.total_minor<>p.gross_minor THEN
      RAISE EXCEPTION 'successful payout requires one bound journal' USING ERRCODE='23514';
    END IF;
  ELSIF r.state<>'RELEASED' OR has_journal THEN
    RAISE EXCEPTION 'negative terminal payout must release without journal' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER payout_final_on_hold_update AFTER UPDATE ON ledger.reservation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE CONSTRAINT TRIGGER payout_final_on_resolution AFTER INSERT ON ledger.payout_resolution
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_payout_admission();
CREATE TRIGGER observation_immutable BEFORE UPDATE OR DELETE ON ledger.provider_observation
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER receipt_immutable BEFORE UPDATE OR DELETE ON ledger.observation_receipt
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER quarantine_immutable BEFORE UPDATE OR DELETE ON ledger.observation_quarantine
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER incident_immutable BEFORE UPDATE OR DELETE ON ledger.payout_incident
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER resolution_immutable BEFORE UPDATE OR DELETE ON ledger.payout_resolution
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

REVOKE ALL ON ledger.provider_observation,ledger.observation_receipt,ledger.observation_quarantine,
  ledger.payout_incident,ledger.payout_resolution,ledger.related_payout_incident FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.protect_reservation_transition() FROM PUBLIC;
GRANT SELECT ON ledger.provider_observation,ledger.observation_receipt,ledger.observation_quarantine,
  ledger.payout_incident,ledger.payout_resolution,ledger.related_payout_incident
  TO ledger_app,ledger_reader,ledger_maintenance,ledger_worker;
GRANT INSERT ON ledger.provider_observation,ledger.observation_receipt,ledger.observation_quarantine,
  ledger.payout_incident,ledger.payout_resolution,ledger.reservation_event,ledger.journal,ledger.journal_entry
  TO ledger_worker;
GRANT UPDATE (state,version) ON ledger.reservation TO ledger_worker;
GRANT UPDATE (knowledge,version,source_sequence) ON ledger.payout TO ledger_worker;
GRANT UPDATE (debits,credits,reserved,version) ON ledger.account_balance TO ledger_worker;
GRANT SELECT ON ledger.journal_entry TO ledger_worker;
