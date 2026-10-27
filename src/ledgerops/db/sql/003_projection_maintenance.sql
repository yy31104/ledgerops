-- R2-PR02. The caller provisions a separate non-owner ledger_maintenance login.
-- These are immutable repair audit rows, not activatable balance generations.
ALTER TABLE ledger.workspace
  ADD COLUMN projection_epoch bigint NOT NULL DEFAULT 1 CHECK (projection_epoch > 0),
  ADD COLUMN current_freeze_id uuid;

CREATE TABLE ledger.maintenance_freeze (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  freeze_id uuid NOT NULL,
  actor_id uuid NOT NULL,
  reason text NOT NULL CHECK (octet_length(reason) BETWEEN 1 AND 256),
  frozen_at timestamptz NOT NULL DEFAULT now(),
  resumed_at timestamptz,
  resumed_by uuid,
  resume_reason text CHECK (octet_length(resume_reason) BETWEEN 1 AND 256),
  PRIMARY KEY (workspace_id, freeze_id),
  CHECK ((resumed_at IS NULL AND resumed_by IS NULL AND resume_reason IS NULL)
      OR (resumed_at IS NOT NULL AND resumed_by IS NOT NULL AND resume_reason IS NOT NULL))
);
CREATE UNIQUE INDEX maintenance_one_open_freeze ON ledger.maintenance_freeze(workspace_id)
  WHERE resumed_at IS NULL;

ALTER TABLE ledger.workspace ADD CONSTRAINT workspace_current_freeze_fk
  FOREIGN KEY (id, current_freeze_id)
  REFERENCES ledger.maintenance_freeze(workspace_id, freeze_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE ledger.projection_repair (
  workspace_id uuid NOT NULL,
  repair_id uuid NOT NULL,
  freeze_id uuid NOT NULL,
  actor_id uuid NOT NULL,
  reason text NOT NULL CHECK (octet_length(reason) BETWEEN 1 AND 256),
  preview_export_id uuid NOT NULL,
  source_witness text NOT NULL CHECK (octet_length(source_witness) > 0),
  old_epoch bigint NOT NULL CHECK (old_epoch > 0),
  new_epoch bigint NOT NULL CHECK (new_epoch::numeric = old_epoch::numeric + 1),
  account_count integer NOT NULL CHECK (account_count BETWEEN 0 AND 10000),
  applied_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, repair_id),
  FOREIGN KEY (workspace_id, freeze_id)
    REFERENCES ledger.maintenance_freeze(workspace_id, freeze_id)
);

CREATE TABLE ledger.projection_repair_row (
  workspace_id uuid NOT NULL,
  repair_id uuid NOT NULL,
  account_id uuid NOT NULL,
  normal_side text NOT NULL,
  before_debits ledger.minor_total,
  before_credits ledger.minor_total,
  before_reserved ledger.minor_total,
  before_booked numeric,
  before_version bigint,
  after_debits ledger.minor_total NOT NULL,
  after_credits ledger.minor_total NOT NULL,
  after_reserved ledger.minor_total NOT NULL DEFAULT 0 CHECK (after_reserved = 0),
  after_booked numeric NOT NULL,
  after_version bigint NOT NULL CHECK (after_version >= 1),
  PRIMARY KEY (workspace_id, repair_id, account_id),
  FOREIGN KEY (workspace_id, repair_id)
    REFERENCES ledger.projection_repair(workspace_id, repair_id),
  FOREIGN KEY (workspace_id, account_id, normal_side)
    REFERENCES ledger.account(workspace_id, id, normal_side),
  CHECK (after_booked = CASE normal_side WHEN 'D' THEN after_debits - after_credits
                       ELSE after_credits - after_debits END
         AND after_booked >= after_reserved),
  CHECK (
    (before_debits IS NULL AND before_credits IS NULL AND before_reserved IS NULL
     AND before_booked IS NULL AND before_version IS NULL AND after_version = 1)
    OR
    (before_debits IS NOT NULL AND before_credits IS NOT NULL AND before_reserved IS NOT NULL
     AND before_booked IS NOT NULL AND before_version IS NOT NULL
     AND before_reserved = 0 AND before_version >= 0
     AND before_booked = CASE normal_side WHEN 'D' THEN before_debits - before_credits
                         ELSE before_credits - before_debits END
     AND before_booked >= before_reserved
     AND after_version::numeric = before_version::numeric + 1)
  )
);

CREATE FUNCTION ledger.protect_freeze_resume() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF OLD.resumed_at IS NOT NULL OR NEW.resumed_at IS NULL
     OR NEW.resumed_by IS NULL OR NEW.resume_reason IS NULL
     OR (NEW.workspace_id, NEW.freeze_id, NEW.actor_id, NEW.reason, NEW.frozen_at)
        IS DISTINCT FROM
        (OLD.workspace_id, OLD.freeze_id, OLD.actor_id, OLD.reason, OLD.frozen_at) THEN
    RAISE EXCEPTION 'freeze allows only one transition to resumed' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION ledger.assert_repair_matches_declaration() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE declared_accounts integer; actual_accounts bigint;
BEGIN
  SELECT account_count INTO declared_accounts FROM ledger.projection_repair
    WHERE workspace_id = NEW.workspace_id AND repair_id = NEW.repair_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'repair header is missing' USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO actual_accounts FROM ledger.projection_repair_row
    WHERE workspace_id = NEW.workspace_id AND repair_id = NEW.repair_id;
  IF actual_accounts <> declared_accounts THEN
    RAISE EXCEPTION 'repair does not match immutable declaration' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER maintenance_freeze_transition BEFORE UPDATE ON ledger.maintenance_freeze
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_freeze_resume();
CREATE TRIGGER maintenance_freeze_no_delete BEFORE DELETE ON ledger.maintenance_freeze
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER projection_repair_immutable BEFORE UPDATE OR DELETE ON ledger.projection_repair
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER projection_repair_row_immutable BEFORE UPDATE OR DELETE ON ledger.projection_repair_row
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE CONSTRAINT TRIGGER repair_declared_on_header AFTER INSERT ON ledger.projection_repair
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_repair_matches_declaration();
CREATE CONSTRAINT TRIGGER repair_declared_on_row AFTER INSERT ON ledger.projection_repair_row
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_repair_matches_declaration();

REVOKE ALL ON ledger.maintenance_freeze, ledger.projection_repair,
  ledger.projection_repair_row FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.protect_freeze_resume(),
  ledger.assert_repair_matches_declaration() FROM PUBLIC;
GRANT SELECT ON ledger.maintenance_freeze, ledger.projection_repair,
  ledger.projection_repair_row TO ledger_reader;

GRANT USAGE ON SCHEMA ledger TO ledger_maintenance;
GRANT SELECT ON ledger.workspace, ledger.account, ledger.account_balance,
  ledger.operation, ledger.request_result, ledger.journal, ledger.journal_entry,
  ledger.schema_migration, ledger.maintenance_freeze, ledger.projection_repair,
  ledger.projection_repair_row TO ledger_maintenance;
GRANT UPDATE (mode, current_freeze_id, projection_epoch) ON ledger.workspace TO ledger_maintenance;
GRANT INSERT (workspace_id, account_id, normal_side, debits, credits, version)
  ON ledger.account_balance TO ledger_maintenance;
GRANT UPDATE (debits, credits, version) ON ledger.account_balance TO ledger_maintenance;
GRANT INSERT ON ledger.maintenance_freeze, ledger.projection_repair,
  ledger.projection_repair_row TO ledger_maintenance;
GRANT UPDATE (resumed_at, resumed_by, resume_reason)
  ON ledger.maintenance_freeze TO ledger_maintenance;
