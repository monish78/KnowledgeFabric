-- Graphbase initial schema.
-- Every table carries the audit columns created_at, updated_at, modified_by.
-- updated_at is maintained by trigger; modified_by is set by the application
-- ('system' for automated jobs).

-- Trigger arguments name columns whose changes alone do not count as a
-- modification (e.g. users.last_login_at), so logins don't rewrite the audit.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
DECLARE
    old_row jsonb := to_jsonb(OLD) - 'updated_at' - 'modified_by';
    new_row jsonb := to_jsonb(NEW) - 'updated_at' - 'modified_by';
    col text;
BEGIN
    FOREACH col IN ARRAY coalesce(TG_ARGV, '{}'::text[]) LOOP
        old_row := old_row - col;
        new_row := new_row - col;
    END LOOP;
    IF old_row IS DISTINCT FROM new_row THEN
        NEW.updated_at := now();
    ELSE
        NEW.updated_at := OLD.updated_at;
        NEW.modified_by := OLD.modified_by;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------- users
CREATE TABLE users (
    user_id        text PRIMARY KEY,
    display_name   text NOT NULL,
    email          text,
    password_hash  text,                           -- null for Keycloak users
    auth_source    text NOT NULL DEFAULT 'local' CHECK (auth_source IN ('local', 'keycloak')),
    is_active      boolean NOT NULL DEFAULT true,
    last_login_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    modified_by    text NOT NULL
);

-- ---------------------------------------------------------------- kb_catalog
-- One row per knowledge base (knowledge_bases has one row per user per base).
CREATE TABLE kb_catalog (
    kb_name          text PRIMARY KEY CHECK (kb_name ~ '^[a-z][a-z0-9_]{2,62}$'),
    kb_type          text NOT NULL CHECK (kb_type IN ('graph', 'rag')),
    domain           text NOT NULL,
    sub_domain       text NOT NULL,
    owner_id         text NOT NULL REFERENCES users (user_id),
    status           text NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft', 'extracting', 'awaiting_review', 'building', 'ingesting', 'ready', 'failed')),
    status_detail    text,
    storage_ref      text,        -- Neo4j database / KB label, or Chroma collection
    draft_schema     jsonb,       -- LLM output, edited on the Review screen
    approved_schema  jsonb,       -- set on Submit; reused by add-data
    approved_cypher  text,
    approved_by      text REFERENCES users (user_id),
    approved_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    modified_by      text NOT NULL
);

-- ---------------------------------------------------------------- knowledge_bases
CREATE TABLE knowledge_bases (
    user_id      text NOT NULL REFERENCES users (user_id),
    kb_name      text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE,
    kb_type      text NOT NULL CHECK (kb_type IN ('graph', 'rag')),
    domain       text NOT NULL,
    sub_domain   text NOT NULL,
    access       text NOT NULL CHECK (access IN ('owner', 'user')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    modified_by  text NOT NULL,
    PRIMARY KEY (user_id, kb_name)
);
CREATE INDEX knowledge_bases_kb_idx ON knowledge_bases (kb_name);

-- ---------------------------------------------------------------- kb_access (audit trail)
CREATE TABLE kb_access (
    id           bigserial PRIMARY KEY,
    kb_name      text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE,
    user_id      text NOT NULL REFERENCES users (user_id),
    role         text NOT NULL CHECK (role IN ('owner', 'user')),
    granted_by   text NOT NULL,                    -- 'system' for the owner row
    granted_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,                      -- null while active
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    modified_by  text NOT NULL
);
-- at most one active grant per user per KB
CREATE UNIQUE INDEX kb_access_active_uq ON kb_access (kb_name, user_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------- kb_pii_fields
CREATE TABLE kb_pii_fields (
    id               bigserial PRIMARY KEY,
    kb_name          text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE,
    node_label       text,        -- graph KBs
    property_name    text,        -- graph KBs
    source_document  text,        -- RAG KBs
    pii_category     text NOT NULL,
    sensitivity      text NOT NULL CHECK (sensitivity IN ('high', 'medium', 'low')),
    confidence       numeric(4, 3) CHECK (confidence BETWEEN 0 AND 1),
    occurrences      integer,     -- RAG: matches found in the document
    reason           text,        -- never the raw PII value
    detected_by      text NOT NULL DEFAULT 'llm',
    status           text NOT NULL DEFAULT 'detected' CHECK (status IN ('detected', 'confirmed', 'dismissed')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    modified_by      text NOT NULL,
    CHECK ((node_label IS NOT NULL AND property_name IS NOT NULL) OR source_document IS NOT NULL)
);
CREATE UNIQUE INDEX kb_pii_graph_uq ON kb_pii_fields (kb_name, node_label, property_name)
    WHERE source_document IS NULL;
CREATE UNIQUE INDEX kb_pii_doc_uq ON kb_pii_fields (kb_name, source_document, pii_category)
    WHERE source_document IS NOT NULL;

-- ---------------------------------------------------------------- jobs (progress for screen 3 and upload chips)
CREATE TABLE jobs (
    id           bigserial PRIMARY KEY,
    kb_name      text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE,
    job_type     text NOT NULL CHECK (job_type IN ('graph_extract', 'graph_build', 'rag_ingest', 'add_data')),
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    source_file  text,
    steps        jsonb NOT NULL DEFAULT '[]',     -- [{name, status, detail}]
    progress     numeric(5, 2) NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    error        text,
    started_by   text NOT NULL REFERENCES users (user_id),
    started_at   timestamptz,
    finished_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    modified_by  text NOT NULL
);
CREATE INDEX jobs_kb_idx ON jobs (kb_name, created_at DESC);

-- ---------------------------------------------------------------- pipeline_runs (data loads, screen 6)
CREATE TABLE pipeline_runs (
    id                     bigserial PRIMARY KEY,
    kb_name                text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE,
    run_no                 integer NOT NULL,
    job_id                 bigint REFERENCES jobs (id),
    run_type               text NOT NULL CHECK (run_type IN ('initial_build', 'add_data', 'rag_ingest')),
    source_file            text NOT NULL,
    status                 text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    rows_total             integer,
    rows_loaded            integer,
    rows_rejected          integer,
    nodes_created          integer,
    relationships_created  integer,
    chunks_added           integer,
    summary                text,
    rejected_report        jsonb,                 -- [{sheet, row, reason}]
    started_by             text NOT NULL REFERENCES users (user_id),
    started_at             timestamptz NOT NULL DEFAULT now(),
    finished_at            timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    modified_by            text NOT NULL,
    UNIQUE (kb_name, run_no)
);

-- ---------------------------------------------------------------- updated_at triggers
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['kb_catalog', 'knowledge_bases', 'kb_access', 'kb_pii_fields', 'jobs', 'pipeline_runs']
    LOOP
        EXECUTE format('CREATE TRIGGER %I_set_updated_at BEFORE UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t, t);
    END LOOP;
END $$;
CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at('last_login_at');
