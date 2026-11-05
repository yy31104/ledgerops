-- R1-PR01. Business admission is Python's responsibility in the next slice.
CREATE DOMAIN ledger.minor_total AS numeric
  CHECK (VALUE BETWEEN 0 AND 99999999999999999999999999999999999999
         AND VALUE = trunc(VALUE));

CREATE TABLE ledger.workspace (
  id uuid PRIMARY KEY,
  currency text NOT NULL DEFAULT 'PLN' CHECK (currency = 'PLN'),
  schema_epoch bigint NOT NULL DEFAULT 1 CHECK (schema_epoch > 0),
  mode text NOT NULL DEFAULT 'NORMAL' CHECK (mode IN ('NORMAL', 'FROZEN', 'RESTORE'))
);

CREATE TABLE ledger.account (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  id uuid NOT NULL,
  owner_id uuid NOT NULL,
  class text NOT NULL,
  normal_side text NOT NULL,
  currency text NOT NULL DEFAULT 'PLN' CHECK (currency = 'PLN'),
  enabled boolean NOT NULL DEFAULT true,
  PRIMARY KEY (workspace_id, id),
  UNIQUE (workspace_id, id, normal_side),
  UNIQUE (workspace_id, class, owner_id),
  CHECK ((class = 'SYNTHETIC_CLEARING' AND normal_side = 'D')
      OR (class IN ('CUSTOMER_LIABILITY', 'MERCHANT_LIABILITY') AND normal_side = 'C'))
);

CREATE TABLE ledger.account_balance (
  workspace_id uuid NOT NULL,
  account_id uuid NOT NULL,
  normal_side text NOT NULL,
  debits ledger.minor_total NOT NULL DEFAULT 0,
  credits ledger.minor_total NOT NULL DEFAULT 0,
  reserved ledger.minor_total NOT NULL DEFAULT 0,
  booked numeric GENERATED ALWAYS AS
    (CASE normal_side WHEN 'D' THEN debits - credits ELSE credits - debits END) STORED,
  version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
  PRIMARY KEY (workspace_id, account_id),
  FOREIGN KEY (workspace_id, account_id, normal_side)
    REFERENCES ledger.account(workspace_id, id, normal_side),
  CHECK ((CASE normal_side WHEN 'D' THEN debits - credits ELSE credits - debits END)
         >= reserved)
);

CREATE TABLE ledger.operation (
  workspace_id uuid NOT NULL REFERENCES ledger.workspace(id),
  operation_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('OPENING', 'PURCHASE')),
  payload bytea NOT NULL CHECK (octet_length(payload) > 0),
  payload_sha256 bytea NOT NULL CHECK (payload_sha256 = sha256(payload)),
  admission text NOT NULL DEFAULT 'IN_PROGRESS',
  http_status integer,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, operation_id),
  UNIQUE (workspace_id, operation_id, kind),
  CHECK (
    (admission = 'IN_PROGRESS' AND http_status IS NULL AND response_body IS NULL)
    OR
    (admission IN ('ACCEPTED', 'REJECTED') AND http_status IS NOT NULL
      AND response_body IS NOT NULL AND jsonb_typeof(response_body) = 'object'
      AND ((admission = 'ACCEPTED' AND http_status = 201)
        OR (admission = 'REJECTED' AND http_status = 422)))
  )
);

CREATE TABLE ledger.request_result (
  workspace_id uuid NOT NULL,
  client_id uuid NOT NULL,
  route_family text NOT NULL CHECK (route_family = 'purchase:v1'),
  key text NOT NULL CHECK (octet_length(key) BETWEEN 1 AND 128),
  operation_id uuid NOT NULL,
  payload bytea NOT NULL CHECK (octet_length(payload) > 0),
  payload_sha256 bytea NOT NULL CHECK (payload_sha256 = sha256(payload)),
  http_status integer,
  response_body jsonb,
  PRIMARY KEY (workspace_id, client_id, route_family, key),
  FOREIGN KEY (workspace_id, operation_id)
    REFERENCES ledger.operation(workspace_id, operation_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK ((http_status IS NULL AND response_body IS NULL)
      OR (http_status IS NOT NULL AND http_status IN (201, 422)
          AND response_body IS NOT NULL AND jsonb_typeof(response_body) = 'object'))
);
CREATE INDEX request_result_operation_idx ON ledger.request_result(workspace_id, operation_id);

CREATE TABLE ledger.journal (
  workspace_id uuid NOT NULL,
  journal_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  kind text NOT NULL,
  line_count smallint NOT NULL CHECK (line_count BETWEEN 2 AND 16),
  total_minor ledger.minor_total NOT NULL CHECK (total_minor > 0),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, journal_id),
  UNIQUE (workspace_id, operation_id),
  FOREIGN KEY (workspace_id, operation_id, kind)
    REFERENCES ledger.operation(workspace_id, operation_id, kind)
);

CREATE TABLE ledger.journal_entry (
  workspace_id uuid NOT NULL,
  journal_id uuid NOT NULL,
  line_no smallint NOT NULL CHECK (line_no BETWEEN 1 AND 16),
  account_id uuid NOT NULL,
  side text NOT NULL CHECK (side IN ('D', 'C')),
  amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 9000000000000000),
  PRIMARY KEY (workspace_id, journal_id, line_no),
  UNIQUE (workspace_id, journal_id, account_id),
  FOREIGN KEY (workspace_id, journal_id)
    REFERENCES ledger.journal(workspace_id, journal_id),
  FOREIGN KEY (workspace_id, account_id)
    REFERENCES ledger.account(workspace_id, id)
);
CREATE INDEX journal_entry_account_idx ON ledger.journal_entry(workspace_id, account_id, journal_id);

CREATE FUNCTION ledger.reject_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;

CREATE FUNCTION ledger.protect_account_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF (NEW.workspace_id, NEW.id, NEW.owner_id, NEW.class, NEW.normal_side, NEW.currency)
     IS DISTINCT FROM
     (OLD.workspace_id, OLD.id, OLD.owner_id, OLD.class, OLD.normal_side, OLD.currency) THEN
    RAISE EXCEPTION 'account identity is immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION ledger.protect_operation_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF OLD.admission <> 'IN_PROGRESS' OR NEW.admission NOT IN ('ACCEPTED', 'REJECTED')
     OR (NEW.workspace_id, NEW.operation_id, NEW.kind, NEW.payload,
         NEW.payload_sha256, NEW.created_at) IS DISTINCT FROM
        (OLD.workspace_id, OLD.operation_id, OLD.kind, OLD.payload,
         OLD.payload_sha256, OLD.created_at) THEN
    RAISE EXCEPTION 'operation allows only one transition to terminal' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION ledger.protect_request_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF OLD.http_status IS NOT NULL OR NEW.http_status IS NULL OR NEW.response_body IS NULL
     OR (NEW.workspace_id, NEW.client_id, NEW.route_family, NEW.key, NEW.operation_id,
         NEW.payload, NEW.payload_sha256) IS DISTINCT FROM
        (OLD.workspace_id, OLD.client_id, OLD.route_family, OLD.key, OLD.operation_id,
         OLD.payload, OLD.payload_sha256) THEN
    RAISE EXCEPTION 'request allows only one transition to terminal' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

-- Deferred events contain the OLD event's NEW tuple: query the final row instead.
CREATE FUNCTION ledger.assert_operation_terminal() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE state text;
BEGIN
  SELECT admission INTO state FROM ledger.operation
    WHERE workspace_id = NEW.workspace_id AND operation_id = NEW.operation_id;
  IF NOT FOUND OR state = 'IN_PROGRESS' THEN
    RAISE EXCEPTION 'operation is incomplete' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION ledger.assert_request_terminal() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE status integer;
BEGIN
  SELECT http_status INTO status FROM ledger.request_result
    WHERE workspace_id = NEW.workspace_id AND client_id = NEW.client_id
      AND route_family = NEW.route_family AND key = NEW.key;
  IF NOT FOUND OR status IS NULL THEN
    RAISE EXCEPTION 'request result is incomplete' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION ledger.assert_journal_matches_declaration() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE declared_lines smallint; declared_total numeric;
        actual_lines bigint; debit_total numeric; credit_total numeric;
BEGIN
  SELECT line_count, total_minor INTO declared_lines, declared_total FROM ledger.journal
    WHERE workspace_id = NEW.workspace_id AND journal_id = NEW.journal_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'journal header is missing' USING ERRCODE = '23514';
  END IF;
  SELECT count(*), coalesce(sum(amount_minor) FILTER (WHERE side = 'D'), 0),
         coalesce(sum(amount_minor) FILTER (WHERE side = 'C'), 0)
    INTO actual_lines, debit_total, credit_total FROM ledger.journal_entry
    WHERE workspace_id = NEW.workspace_id AND journal_id = NEW.journal_id;
  IF actual_lines <> declared_lines OR debit_total <> declared_total
     OR credit_total <> declared_total THEN
    RAISE EXCEPTION 'journal does not match immutable declaration' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER account_identity BEFORE UPDATE ON ledger.account
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_account_identity();
CREATE TRIGGER account_no_delete BEFORE DELETE ON ledger.account
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER journal_immutable BEFORE UPDATE OR DELETE ON ledger.journal
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER journal_entry_immutable BEFORE UPDATE OR DELETE ON ledger.journal_entry
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER operation_no_delete BEFORE DELETE ON ledger.operation
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER request_no_delete BEFORE DELETE ON ledger.request_result
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();
CREATE TRIGGER operation_transition BEFORE UPDATE ON ledger.operation
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_operation_transition();
CREATE TRIGGER request_transition BEFORE UPDATE ON ledger.request_result
  FOR EACH ROW EXECUTE FUNCTION ledger.protect_request_transition();

CREATE CONSTRAINT TRIGGER journal_declared_on_header AFTER INSERT ON ledger.journal
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_journal_matches_declaration();
CREATE CONSTRAINT TRIGGER journal_declared_on_line AFTER INSERT ON ledger.journal_entry
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_journal_matches_declaration();
CREATE CONSTRAINT TRIGGER operation_terminal AFTER INSERT OR UPDATE ON ledger.operation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_operation_terminal();
CREATE CONSTRAINT TRIGGER request_terminal AFTER INSERT OR UPDATE ON ledger.request_result
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION ledger.assert_request_terminal();

REVOKE ALL ON ALL TABLES IN SCHEMA ledger FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ledger FROM PUBLIC;
GRANT USAGE ON SCHEMA ledger TO ledger_app;
GRANT SELECT ON ledger.workspace, ledger.account, ledger.account_balance,
  ledger.operation, ledger.request_result, ledger.journal, ledger.journal_entry TO ledger_app;
GRANT INSERT ON ledger.operation, ledger.request_result, ledger.journal,
  ledger.journal_entry TO ledger_app;
GRANT UPDATE (debits, credits, reserved, version) ON ledger.account_balance TO ledger_app;
GRANT UPDATE (admission, http_status, response_body) ON ledger.operation TO ledger_app;
GRANT UPDATE (http_status, response_body) ON ledger.request_result TO ledger_app;
