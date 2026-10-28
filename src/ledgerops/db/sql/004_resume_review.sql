-- Resume gates structure/projection only; intent/reservation findings stay visible.
-- Legacy closed freezes have no stored report and remain readable as such.
ALTER TABLE ledger.maintenance_freeze ADD COLUMN resume_report jsonb
  CHECK (resume_report IS NULL OR jsonb_typeof(resume_report) = 'object');

CREATE OR REPLACE FUNCTION ledger.protect_freeze_resume() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF OLD.resumed_at IS NOT NULL OR NEW.resumed_at IS NULL
     OR NEW.resumed_by IS NULL OR NEW.resume_reason IS NULL
     OR NEW.resume_report IS NULL
     OR NEW.resume_report->'structure_ok' IS DISTINCT FROM 'true'::jsonb
     OR NEW.resume_report->'projection_ok' IS DISTINCT FROM 'true'::jsonb
     OR (NEW.workspace_id, NEW.freeze_id, NEW.actor_id, NEW.reason, NEW.frozen_at)
        IS DISTINCT FROM
        (OLD.workspace_id, OLD.freeze_id, OLD.actor_id, OLD.reason, OLD.frozen_at) THEN
    RAISE EXCEPTION 'freeze allows one resume with structure/projection report'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

GRANT UPDATE (resume_report) ON ledger.maintenance_freeze TO ledger_maintenance;
