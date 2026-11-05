-- Reconciliation evidence has no financial write authority. Runtime credentials
-- for ledger_reconciler are provisioned separately by the operator.
CREATE TABLE ledger.recon_registry_guard (
  workspace_id uuid PRIMARY KEY REFERENCES ledger.workspace(id),
  evidence_epoch bigint NOT NULL DEFAULT 0 CHECK (evidence_epoch >= 0)
);

CREATE TABLE ledger.recon_object (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  byte_length bigint NOT NULL CHECK (byte_length BETWEEN 1 AND 67108864),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id,sha256)
);

CREATE TABLE ledger.recon_run (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  run_id uuid NOT NULL,
  provider_namespace uuid NOT NULL,
  business_date date NOT NULL,
  manifest bytea NOT NULL CHECK (octet_length(manifest) BETWEEN 1 AND 4194304),
  manifest_sha256 text GENERATED ALWAYS AS (encode(sha256(manifest),'hex')) STORED,
  partition_plan bytea NOT NULL CHECK (octet_length(partition_plan) BETWEEN 1 AND 16777216),
  plan_sha256 text GENERATED ALWAYS AS (encode(sha256(partition_plan),'hex')) STORED,
  partition_count integer NOT NULL CHECK (partition_count BETWEEN 1 AND 4096),
  started_epoch bigint NOT NULL CHECK (started_epoch >= 0),
  expected_current uuid,
  state text NOT NULL DEFAULT 'STAGING' CHECK (state IN ('STAGING','COMPLETE','FAILED')),
  result_sha256 text,
  semantic_sha256 text CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
  published boolean,
  published_epoch bigint CHECK (published_epoch > 0),
  failure_code text CHECK (failure_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at timestamptz,
  PRIMARY KEY (workspace_id,run_id),
  FOREIGN KEY (workspace_id,result_sha256) REFERENCES ledger.recon_object(workspace_id,sha256),
  FOREIGN KEY (workspace_id,expected_current) REFERENCES ledger.recon_run(workspace_id,run_id),
  CHECK (
    (state='STAGING' AND result_sha256 IS NULL AND semantic_sha256 IS NULL AND published IS NULL
      AND published_epoch IS NULL AND failure_code IS NULL AND finished_at IS NULL)
    OR (state='FAILED' AND result_sha256 IS NULL AND semantic_sha256 IS NULL AND published IS NULL
      AND published_epoch IS NULL AND failure_code IS NOT NULL AND finished_at IS NOT NULL)
    OR (state='COMPLETE' AND result_sha256 IS NOT NULL AND semantic_sha256 IS NOT NULL
      AND published IS NOT NULL AND (published=(published_epoch IS NOT NULL))
      AND failure_code IS NULL AND finished_at IS NOT NULL)
  )
);

CREATE TABLE ledger.recon_partition (
  workspace_id uuid NOT NULL,
  run_id uuid NOT NULL,
  partition_id integer NOT NULL CHECK (partition_id BETWEEN 0 AND 4095),
  generation integer NOT NULL DEFAULT 1 CHECK (generation=1),
  declaration bytea NOT NULL CHECK (octet_length(declaration) BETWEEN 1 AND 16777216),
  declaration_sha256 text GENERATED ALWAYS AS (encode(sha256(declaration),'hex')) STORED,
  PRIMARY KEY (workspace_id,run_id,partition_id),
  FOREIGN KEY (workspace_id,run_id) REFERENCES ledger.recon_run(workspace_id,run_id)
);

CREATE TABLE ledger.recon_accepted_result (
  workspace_id uuid NOT NULL,
  run_id uuid NOT NULL,
  partition_id integer NOT NULL,
  generation integer NOT NULL CHECK (generation=1),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
  result_sha256 text NOT NULL,
  semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
  accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id,run_id,partition_id),
  FOREIGN KEY (workspace_id,run_id,partition_id)
    REFERENCES ledger.recon_partition(workspace_id,run_id,partition_id),
  FOREIGN KEY (workspace_id,result_sha256) REFERENCES ledger.recon_object(workspace_id,sha256)
);

CREATE TABLE ledger.recon_current (
  workspace_id uuid NOT NULL,
  provider_namespace uuid NOT NULL,
  business_date date NOT NULL,
  run_id uuid NOT NULL,
  evidence_epoch bigint NOT NULL CHECK (evidence_epoch > 0),
  PRIMARY KEY (workspace_id,provider_namespace,business_date),
  FOREIGN KEY (workspace_id,run_id) REFERENCES ledger.recon_run(workspace_id,run_id)
);

CREATE TABLE ledger.evidence_pin (
  workspace_id uuid NOT NULL,
  pin_id uuid NOT NULL,
  object_sha256 text NOT NULL,
  owner_type text NOT NULL CHECK (owner_type IN ('PUBLISHER','RUN','READER')),
  owner_id uuid NOT NULL,
  run_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id,pin_id),
  UNIQUE (workspace_id,object_sha256,owner_type,owner_id),
  FOREIGN KEY (workspace_id,object_sha256) REFERENCES ledger.recon_object(workspace_id,sha256),
  FOREIGN KEY (workspace_id,run_id) REFERENCES ledger.recon_run(workspace_id,run_id),
  CHECK ((owner_type='PUBLISHER' AND run_id IS NULL)
    OR (owner_type='RUN' AND run_id IS NOT NULL AND run_id=owner_id)
    OR (owner_type='READER' AND run_id IS NOT NULL))
);

CREATE INDEX evidence_pin_owner_lookup
  ON ledger.evidence_pin(workspace_id,owner_type,owner_id,run_id);
CREATE INDEX evidence_pin_run_lookup
  ON ledger.evidence_pin(workspace_id,run_id,object_sha256) WHERE owner_type='RUN';

CREATE FUNCTION ledger.protect_recon_run() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF OLD.state<>'STAGING' OR NEW.state NOT IN ('COMPLETE','FAILED')
    OR (to_jsonb(NEW)-ARRAY['state','result_sha256','semantic_sha256','published','published_epoch',
        'failure_code','finished_at','manifest_sha256','plan_sha256']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['state','result_sha256','semantic_sha256','published','published_epoch',
        'failure_code','finished_at','manifest_sha256','plan_sha256']) THEN
    RAISE EXCEPTION 'reconciliation run permits one terminal transition' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recon_run_transition BEFORE UPDATE ON ledger.recon_run
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_recon_run();
CREATE TRIGGER recon_run_no_delete BEFORE DELETE ON ledger.recon_run
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

CREATE FUNCTION ledger.assert_recon_coverage() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE parent ledger.recon_run%ROWTYPE; actual bigint; accepted bigint;
BEGIN
  SELECT * INTO parent FROM ledger.recon_run WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id;
  SELECT count(*) INTO actual FROM ledger.recon_partition
    WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id;
  IF parent.run_id IS NULL OR actual<>parent.partition_count THEN
    RAISE EXCEPTION 'reconciliation partition declaration is incomplete' USING ERRCODE='23514';
  END IF;
  IF parent.state='COMPLETE' THEN
    SELECT count(*) INTO accepted FROM ledger.recon_accepted_result
      WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id;
    IF accepted<>actual THEN
      RAISE EXCEPTION 'completed reconciliation requires every accepted partition' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER recon_run_coverage AFTER INSERT OR UPDATE ON ledger.recon_run
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_recon_coverage();
CREATE CONSTRAINT TRIGGER recon_partition_coverage AFTER INSERT ON ledger.recon_partition
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_recon_coverage();

CREATE FUNCTION ledger.protect_recon_acceptance() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE parent ledger.recon_run%ROWTYPE;
BEGIN
  PERFORM 1 FROM ledger.recon_registry_guard WHERE workspace_id=NEW.workspace_id FOR UPDATE;
  SELECT * INTO parent FROM ledger.recon_run
    WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id FOR UPDATE;
  IF NOT FOUND OR parent.state<>'STAGING'
    OR parent.manifest_sha256<>NEW.manifest_sha256 OR parent.plan_sha256<>NEW.plan_sha256 THEN
    RAISE EXCEPTION 'partition result requires its staging run and exact input plan' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recon_acceptance_gate BEFORE INSERT ON ledger.recon_accepted_result
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_recon_acceptance();

CREATE FUNCTION ledger.protect_recon_current() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE parent ledger.recon_run%ROWTYPE; epoch bigint;
BEGIN
  SELECT evidence_epoch INTO epoch FROM ledger.recon_registry_guard
    WHERE workspace_id=NEW.workspace_id FOR UPDATE;
  SELECT * INTO parent FROM ledger.recon_run
    WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id FOR UPDATE;
  IF NOT FOUND OR parent.state<>'COMPLETE' OR NOT parent.published
    OR parent.provider_namespace<>NEW.provider_namespace OR parent.business_date<>NEW.business_date
    OR parent.published_epoch<>NEW.evidence_epoch OR epoch<>NEW.evidence_epoch THEN
    RAISE EXCEPTION 'current reconciliation must reference its completed publication epoch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recon_current_gate BEFORE INSERT OR UPDATE ON ledger.recon_current
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_recon_current();

CREATE FUNCTION ledger.protect_recon_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF NEW.workspace_id<>OLD.workspace_id OR (NEW.evidence_epoch<OLD.evidence_epoch OR NEW.evidence_epoch-OLD.evidence_epoch>1) THEN
    RAISE EXCEPTION 'registry identity is immutable and evidence epoch advances by one' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recon_guard_transition BEFORE UPDATE ON ledger.recon_registry_guard
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_recon_guard();
CREATE TRIGGER recon_guard_no_delete BEFORE DELETE ON ledger.recon_registry_guard
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER recon_current_no_delete BEFORE DELETE ON ledger.recon_current
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

CREATE FUNCTION ledger.protect_reader_pin() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF NEW.owner_type<>'READER' AND NOT pg_has_role(current_user,'ledger_reconciler','USAGE')
    AND NOT pg_has_role(current_user,'ledger_migrator','USAGE') THEN
    RAISE EXCEPTION 'reader may only acquire reader pins' USING ERRCODE='42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER evidence_pin_insert BEFORE INSERT ON ledger.evidence_pin
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_reader_pin();

CREATE FUNCTION ledger.protect_run_pin() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF OLD.owner_type='RUN' THEN
    RAISE EXCEPTION 'retained run pins cannot be released' USING ERRCODE='42501';
  END IF;
  IF OLD.owner_type<>'READER' AND NOT pg_has_role(current_user,'ledger_reconciler','USAGE')
    AND NOT pg_has_role(current_user,'ledger_migrator','USAGE') THEN
    RAISE EXCEPTION 'reader may only release reader pins' USING ERRCODE='42501';
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER evidence_pin_no_update BEFORE UPDATE ON ledger.evidence_pin
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER evidence_pin_delete BEFORE DELETE ON ledger.evidence_pin
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_run_pin();
CREATE TRIGGER recon_object_immutable BEFORE UPDATE OR DELETE ON ledger.recon_object
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER recon_partition_immutable BEFORE UPDATE OR DELETE ON ledger.recon_partition
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER recon_acceptance_immutable BEFORE UPDATE OR DELETE ON ledger.recon_accepted_result
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

REVOKE ALL ON ledger.recon_registry_guard,ledger.recon_object,ledger.recon_run,
  ledger.recon_partition,ledger.recon_accepted_result,ledger.recon_current,ledger.evidence_pin FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.protect_recon_run(),ledger.assert_recon_coverage(),
  ledger.protect_recon_acceptance(),ledger.protect_run_pin(),ledger.protect_recon_current(),
  ledger.protect_recon_guard(),ledger.protect_reader_pin() FROM PUBLIC;
GRANT USAGE ON SCHEMA ledger TO ledger_reconciler;
GRANT SELECT ON ledger.workspace TO ledger_reconciler;
GRANT SELECT,INSERT ON ledger.recon_registry_guard,ledger.recon_object,ledger.recon_run,
  ledger.recon_partition,ledger.recon_accepted_result,ledger.recon_current,ledger.evidence_pin
  TO ledger_reconciler;
GRANT UPDATE(evidence_epoch) ON ledger.recon_registry_guard TO ledger_reconciler;
GRANT UPDATE(generation) ON ledger.recon_partition TO ledger_reconciler;
GRANT UPDATE(state,result_sha256,semantic_sha256,published,published_epoch,failure_code,finished_at)
  ON ledger.recon_run TO ledger_reconciler;
GRANT UPDATE(run_id,evidence_epoch) ON ledger.recon_current TO ledger_reconciler;
GRANT DELETE ON ledger.evidence_pin TO ledger_reconciler;
GRANT SELECT ON ledger.recon_registry_guard,ledger.recon_object,ledger.recon_run,
  ledger.recon_partition,ledger.recon_accepted_result,ledger.recon_current,ledger.evidence_pin TO ledger_reader;
-- UPDATE privilege on protected columns permits SELECT FOR UPDATE. Triggers
-- reject readers changing a run or registry identity.
GRANT UPDATE(workspace_id) ON ledger.recon_registry_guard TO ledger_reader;
GRANT UPDATE(finished_at) ON ledger.recon_run TO ledger_reader;
GRANT INSERT,DELETE ON ledger.evidence_pin TO ledger_reader;
