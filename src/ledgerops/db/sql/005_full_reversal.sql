-- One retained attempt per reversal operation, one successful effect per purchase.
ALTER TABLE ledger.operation DROP CONSTRAINT operation_kind_check;
ALTER TABLE ledger.operation ADD CONSTRAINT operation_kind_check
  CHECK (kind IN ('OPENING', 'PURCHASE', 'REVERSAL'));
ALTER TABLE ledger.request_result DROP CONSTRAINT request_result_route_family_check;
ALTER TABLE ledger.request_result ADD CONSTRAINT request_result_route_family_check
  CHECK (route_family IN ('purchase:v1', 'reversal:v1'));

CREATE TABLE ledger.reversal_attempt (
  workspace_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  original_operation_id uuid NOT NULL,
  kind text NOT NULL DEFAULT 'REVERSAL' CHECK (kind = 'REVERSAL'),
  original_kind text NOT NULL DEFAULT 'PURCHASE' CHECK (original_kind = 'PURCHASE'),
  PRIMARY KEY (workspace_id, operation_id),
  UNIQUE (workspace_id, operation_id, original_operation_id),
  FOREIGN KEY (workspace_id, operation_id, kind)
    REFERENCES ledger.operation(workspace_id, operation_id, kind),
  FOREIGN KEY (workspace_id, original_operation_id, original_kind)
    REFERENCES ledger.operation(workspace_id, operation_id, kind)
);
CREATE INDEX reversal_attempt_original_idx
  ON ledger.reversal_attempt(workspace_id, original_operation_id);

CREATE TABLE ledger.purchase_reversal_effect (
  workspace_id uuid NOT NULL,
  original_operation_id uuid NOT NULL,
  reversal_operation_id uuid NOT NULL,
  PRIMARY KEY (workspace_id, original_operation_id),
  UNIQUE (workspace_id, reversal_operation_id),
  FOREIGN KEY (workspace_id, reversal_operation_id, original_operation_id)
    REFERENCES ledger.reversal_attempt(workspace_id, operation_id, original_operation_id)
);

CREATE FUNCTION ledger.assert_reversal_final() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE reversal_id uuid; reversal ledger.operation%ROWTYPE;
        original_id uuid; original ledger.operation%ROWTYPE;
        original_journal ledger.journal%ROWTYPE; reverse_journal ledger.journal%ROWTYPE;
        has_reverse_journal boolean; has_effect boolean;
BEGIN
  IF TG_TABLE_NAME = 'purchase_reversal_effect' THEN
    reversal_id := NEW.reversal_operation_id;
  ELSE
    reversal_id := NEW.operation_id;
  END IF;
  SELECT * INTO reversal FROM ledger.operation
    WHERE workspace_id = NEW.workspace_id AND operation_id = reversal_id;
  IF NOT FOUND OR reversal.kind <> 'REVERSAL' THEN
    RETURN NULL;
  END IF;
  SELECT original_operation_id INTO original_id FROM ledger.reversal_attempt
    WHERE workspace_id = NEW.workspace_id AND operation_id = reversal_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'reversal requires a retained attempt' USING ERRCODE = '23514';
  END IF;
  SELECT * INTO original FROM ledger.operation
    WHERE workspace_id = NEW.workspace_id AND operation_id = original_id;
  IF NOT FOUND OR original.kind <> 'PURCHASE' OR original.admission <> 'ACCEPTED' THEN
    RAISE EXCEPTION 'original must be an accepted purchase' USING ERRCODE = '23514';
  END IF;
  SELECT * INTO original_journal FROM ledger.journal
    WHERE workspace_id = NEW.workspace_id AND operation_id = original_id;
  IF NOT FOUND OR original_journal.kind <> 'PURCHASE' OR original_journal.line_count <> 2 THEN
    RAISE EXCEPTION 'original purchase journal is incomplete' USING ERRCODE = '23514';
  END IF;
  SELECT * INTO reverse_journal FROM ledger.journal
    WHERE workspace_id = NEW.workspace_id AND operation_id = reversal_id;
  has_reverse_journal := FOUND;
  SELECT EXISTS (SELECT 1 FROM ledger.purchase_reversal_effect
    WHERE workspace_id = NEW.workspace_id AND original_operation_id = original_id
      AND reversal_operation_id = reversal_id) INTO has_effect;
  IF reversal.admission = 'ACCEPTED' THEN
    IF NOT has_effect OR NOT has_reverse_journal OR reverse_journal.kind <> 'REVERSAL'
       OR reverse_journal.line_count <> 2
       OR reverse_journal.total_minor <> original_journal.total_minor THEN
      RAISE EXCEPTION 'accepted reversal requires one matching journal and success relation'
        USING ERRCODE = '23514';
    END IF;
  ELSIF reversal.admission <> 'REJECTED' OR has_effect OR has_reverse_journal THEN
    RAISE EXCEPTION 'rejected reversal must have no financial effect' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER reversal_attempt_immutable BEFORE UPDATE OR DELETE ON ledger.reversal_attempt
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER purchase_reversal_effect_immutable BEFORE UPDATE OR DELETE
  ON ledger.purchase_reversal_effect FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE CONSTRAINT TRIGGER reversal_final_on_operation AFTER INSERT OR UPDATE ON ledger.operation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_reversal_final();
CREATE CONSTRAINT TRIGGER reversal_final_on_attempt AFTER INSERT ON ledger.reversal_attempt
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_reversal_final();
CREATE CONSTRAINT TRIGGER reversal_final_on_effect AFTER INSERT ON ledger.purchase_reversal_effect
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_reversal_final();
CREATE CONSTRAINT TRIGGER reversal_final_on_journal AFTER INSERT ON ledger.journal
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger.assert_reversal_final();

REVOKE ALL ON ledger.reversal_attempt, ledger.purchase_reversal_effect FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger.assert_reversal_final() FROM PUBLIC;
GRANT SELECT ON ledger.reversal_attempt, ledger.purchase_reversal_effect
  TO ledger_app, ledger_reader, ledger_maintenance;
GRANT INSERT ON ledger.reversal_attempt, ledger.purchase_reversal_effect TO ledger_app;
