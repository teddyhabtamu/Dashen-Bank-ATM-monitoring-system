-- EJ shadow staging.
--
-- These tables live in the portal database but are deliberately NOT read by
-- any KPI, report, cash forecast or anomaly query. They are the quarantine
-- between "the journal parser produced a row" and "the portal trusts it as a
-- transaction".
--
-- Promotion path (future, only after reconciliation):
--   ej_transactions -> atm_transactions with source = 'EJ'
--
-- Safe to run repeatedly. Run on UAT with:
--   docker exec -i zabbix-db psql -U zabbix -d portal < scripts/migrate_ej_shadow.sql

BEGIN;

CREATE TABLE IF NOT EXISTS public.ej_parse_runs (
    id              bigserial PRIMARY KEY,
    file_path       text NOT NULL,
    file_sha256     char(64) NOT NULL,
    vendor          varchar(10) NOT NULL,
    encoding        varchar(20),
    sessions        integer NOT NULL DEFAULT 0,
    completed       integer NOT NULL DEFAULT 0,
    approved        integer NOT NULL DEFAULT 0,
    declined        integer NOT NULL DEFAULT 0,
    unknown         integer NOT NULL DEFAULT 0,
    incomplete      integer NOT NULL DEFAULT 0,
    amount_total    numeric(15,2) NOT NULL DEFAULT 0,
    dispensed_total numeric(15,2) NOT NULL DEFAULT 0,
    warnings        jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version  varchar(40) NOT NULL,
    parsed_at       timestamptz NOT NULL DEFAULT now(),
    status          varchar(20) NOT NULL DEFAULT 'ok',
    error           text,
    CONSTRAINT ej_parse_runs_file_sha_key UNIQUE (file_path, file_sha256)
);

COMMENT ON TABLE public.ej_parse_runs IS
  'One row per parsed journal file/hash. Records what the parser saw, including files that failed to parse. Not used by reports.';

-- Fleet UI metadata: a file path alone cannot answer "which ATM is stale?".
-- journal_date/hostname come from the puller's <date>_<HOST>_EJDATA.log name;
-- atm_id comes from the terminal mapping or the registered ej_hostname.
ALTER TABLE public.ej_parse_runs
    ADD COLUMN IF NOT EXISTS atm_id varchar(20),
    ADD COLUMN IF NOT EXISTS hostname varchar(64),
    ADD COLUMN IF NOT EXISTS journal_date date;

UPDATE public.ej_parse_runs
   SET journal_date = COALESCE(
           journal_date,
           (regexp_match(file_path,
             '([0-9]{4}-[0-9]{2}-[0-9]{2})_[^/]*_EJDATA\.log$'))[1]::date),
       hostname = COALESCE(
           hostname,
           upper((regexp_match(file_path,
             '[0-9]{4}-[0-9]{2}-[0-9]{2}_([^/]*)_EJDATA\.log$'))[1]))
 WHERE journal_date IS NULL OR hostname IS NULL;

UPDATE public.ej_parse_runs r
   SET atm_id = a.atm_id
  FROM public.atm_locations a
 WHERE r.atm_id IS NULL
   AND COALESCE(r.hostname, '') <> ''
   AND upper(a.ej_hostname) = upper(r.hostname);

CREATE INDEX IF NOT EXISTS idx_ej_parse_runs_parsed_at
    ON public.ej_parse_runs (parsed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ej_parse_runs_atm_date
    ON public.ej_parse_runs (atm_id, journal_date DESC);
CREATE INDEX IF NOT EXISTS idx_ej_parse_runs_journal_date
    ON public.ej_parse_runs (journal_date DESC);
CREATE INDEX IF NOT EXISTS idx_ej_parse_runs_hostname
    ON public.ej_parse_runs (hostname);

CREATE OR REPLACE VIEW public.ej_parse_latest AS
    SELECT DISTINCT ON (file_path) *
      FROM public.ej_parse_runs
     ORDER BY file_path, parsed_at DESC, id DESC;

COMMENT ON VIEW public.ej_parse_latest IS
  'Latest parse result per journal file, for the read-only EJ Journal page.';

CREATE TABLE IF NOT EXISTS public.ej_transactions (
    id               bigserial PRIMARY KEY,
    transaction_key  text NOT NULL,
    parse_run_id     bigint REFERENCES public.ej_parse_runs(id) ON DELETE SET NULL,
    vendor           varchar(10),
    atm_id           varchar(20),
    terminal_id      varchar(20),
    branch           varchar(100),
    recorded_at      timestamp without time zone NOT NULL,
    started_at       timestamp without time zone,
    ended_at         timestamp without time zone,
    txn_type         varchar(30),
    status           varchar(20),
    amount           numeric(15,2),
    currency         varchar(5),
    dispensed_amount numeric(15,2),
    card_masked      varchar(20),
    auth_code        varchar(20),
    error_code       varchar(10),
    seq_number       varchar(20),
    warnings         jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence       varchar(10),
    parser_version   varchar(40),
    raw_ref          text,
    source_file      text,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ej_transactions_transaction_key_key UNIQUE (transaction_key)
);

COMMENT ON TABLE public.ej_transactions IS
  'Parsed EJ transactions awaiting reconciliation. NOT the official ledger; reports must not read this table.';

-- Session edges.  Nullable on purpose: an old journal, or a session whose
-- end record was not captured, must not receive a made-up interval.  The
-- image page uses these only when both edges are present and on the same day.
ALTER TABLE public.ej_transactions
    ADD COLUMN IF NOT EXISTS started_at timestamp without time zone,
    ADD COLUMN IF NOT EXISTS ended_at timestamp without time zone;

CREATE INDEX IF NOT EXISTS idx_ej_txn_recorded_at
    ON public.ej_transactions (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_ej_txn_atm_time
    ON public.ej_transactions (atm_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_ej_txn_atm_session
    ON public.ej_transactions (atm_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ej_txn_status
    ON public.ej_transactions (status);
CREATE INDEX IF NOT EXISTS idx_ej_txn_warnings
    ON public.ej_transactions USING gin (warnings);

COMMIT;
