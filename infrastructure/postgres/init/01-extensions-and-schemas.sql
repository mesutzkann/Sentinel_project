-- SentinelAI database bootstrap.
--
-- Runs once, on first container start, against the POSTGRES_DB database.
-- Everything lives in one PostgreSQL instance separated by schema:
--
--   sentinel            backend (EF Core migrations own it)
--   rag                 AI service (Alembic owns it)
--   svc_users           sample service: users
--   svc_orders          sample service: orders
--   svc_payments        sample service: payments
--   svc_notifications   sample service: notifications
--
-- One schema per sample service rather than one shared schema, so database-mcp can
-- attribute slow queries, locks and connection pressure to a specific service. Without
-- that attribution, chaos scenarios 1, 2, 3 and 4 all look identical from the database side.

-- ---------------------------------------------------------------- extensions ----

-- Vector similarity for the RAG chunk store (bge-m3 emits 1024 dimensions).
CREATE EXTENSION IF NOT EXISTS vector;

-- Feeds database-mcp's get_slow_queries. Requires shared_preload_libraries, set in compose.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Trigram index support for fuzzy lookups on incident codes and service names.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ------------------------------------------------------------------- schemas ----

CREATE SCHEMA IF NOT EXISTS sentinel;
CREATE SCHEMA IF NOT EXISTS rag;

CREATE SCHEMA IF NOT EXISTS svc_users;
CREATE SCHEMA IF NOT EXISTS svc_orders;
CREATE SCHEMA IF NOT EXISTS svc_payments;
CREATE SCHEMA IF NOT EXISTS svc_notifications;

COMMENT ON SCHEMA sentinel IS 'SentinelAI backend. Owned by EF Core migrations.';
COMMENT ON SCHEMA rag IS 'SentinelAI AI service: documents, chunks, embeddings. Owned by Alembic.';
COMMENT ON SCHEMA svc_users IS 'Sample microservice: users.';
COMMENT ON SCHEMA svc_orders IS 'Sample microservice: orders.';
COMMENT ON SCHEMA svc_payments IS 'Sample microservice: payments.';
COMMENT ON SCHEMA svc_notifications IS 'Sample microservice: notifications.';

-- --------------------------------------------------------------- shared bits ----

-- Incident codes are INC-00001, INC-00002, ... A sequence keeps them gap-tolerant and
-- collision-free without a table lock on every insert.
CREATE SEQUENCE IF NOT EXISTS sentinel.incident_code_seq
    AS BIGINT
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    CACHE 1;

CREATE OR REPLACE FUNCTION sentinel.next_incident_code()
RETURNS TEXT
LANGUAGE SQL
VOLATILE
AS $$
    SELECT 'INC-' || LPAD(nextval('sentinel.incident_code_seq')::TEXT, 5, '0');
$$;

COMMENT ON FUNCTION sentinel.next_incident_code() IS
    'Returns the next incident code, e.g. INC-00042. Called from the backend on insert.';

-- ---------------------------------------------------------------- privileges ----

-- Single dev user owns everything; this is a local development stack, not production.
DO $$
DECLARE
    db_user TEXT := current_user;
    s TEXT;
BEGIN
    FOREACH s IN ARRAY ARRAY[
        'sentinel', 'rag',
        'svc_users', 'svc_orders', 'svc_payments', 'svc_notifications'
    ]
    LOOP
        EXECUTE format('GRANT ALL ON SCHEMA %I TO %I', s, db_user);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL ON TABLES TO %I', s, db_user);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL ON SEQUENCES TO %I', s, db_user);
    END LOOP;
END
$$;
