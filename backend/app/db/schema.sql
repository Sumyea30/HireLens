-- HireLens database schema, target design (v2). PostgreSQL 16 + pgvector.
-- Idempotent: safe to run more than once. Enumerations are text + CHECK (easier to migrate than ENUM types).
-- Never add columns for protected characteristics (age, gender, ethnicity, nationality, religion, marital status) (N20).
-- Personal data lives only in: recruiter, candidate, cv_parsed, transcript_turn, qa_pair, assessment, and vector_item (CV text chunks).

CREATE EXTENSION IF NOT EXISTS vector;

-- ===== Jobs and posting =====================================================
CREATE TABLE IF NOT EXISTS job (
  id          text PRIMARY KEY,
  title       text NOT NULL DEFAULT 'New role',
  status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','awaiting_approval','open','closed','rejected','failed')),
  profile     jsonb NOT NULL DEFAULT '{}',
  weights     jsonb NOT NULL CHECK (jsonb_typeof(weights) = 'object'),
  post_text   text,
  apply_email text,
  channels    jsonb NOT NULL DEFAULT '[]',
  created_at  timestamptz NOT NULL DEFAULT now(),
  closed_at   timestamptz,
  owner_id    text
);
-- Existing databases created before owner_id: keep the column add idempotent.
ALTER TABLE job ADD COLUMN IF NOT EXISTS owner_id text;
-- Existing databases: the original check had no 'failed', so a failed publish could not be saved.
ALTER TABLE job DROP CONSTRAINT IF EXISTS job_status_check;
ALTER TABLE job ADD CONSTRAINT job_status_check CHECK (status IN ('draft','awaiting_approval','open','closed','rejected','failed'));
CREATE INDEX IF NOT EXISTS job_owner ON job (owner_id);

CREATE TABLE IF NOT EXISTS job_requirement (
  job_id   text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  req_id   text NOT NULL,
  position int  NOT NULL,
  text     text NOT NULL,
  kind     text NOT NULL CHECK (kind IN ('required','preferred')),
  category text NOT NULL CHECK (category IN ('skill','experience','qualification','other')),
  PRIMARY KEY (job_id, req_id)
);

CREATE TABLE IF NOT EXISTS posting_decision (
  id            bigserial PRIMARY KEY,
  job_id        text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  action        text NOT NULL CHECK (action IN ('approve','changes','reject')),
  channels      jsonb NOT NULL DEFAULT '[]',
  edited        boolean NOT NULL DEFAULT false,
  approver_role text NOT NULL CHECK (approver_role IN ('interviewer','admin')),
  decided_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publish_result (
  id           bigserial PRIMARY KEY,
  job_id       text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  channel      text NOT NULL,
  status       text NOT NULL CHECK (status IN ('ok','error')),
  public_url   text,
  error_code   text,
  published_at timestamptz NOT NULL DEFAULT now()
);

-- ===== Company policy knowledge base =========================================
CREATE TABLE IF NOT EXISTS policy_document (
  id             text PRIMARY KEY,
  title          text NOT NULL,
  version        text NOT NULL,
  effective_date timestamptz NOT NULL,
  status         text NOT NULL CHECK (status IN ('active','superseded')),
  chunks         int  NOT NULL CHECK (chunks >= 0)
);
-- At most one active version per title.
CREATE UNIQUE INDEX IF NOT EXISTS policy_one_active ON policy_document (title) WHERE status = 'active';

-- ===== Candidates and CV screening ===========================================
CREATE TABLE IF NOT EXISTS candidate (
  id              text PRIMARY KEY,
  job_id          text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  name            text,
  email           text,
  phone           text,
  status          text NOT NULL DEFAULT 'queued' CHECK (status IN
                    ('queued','processing','screened','failed','incomplete',
                     'interview_scheduled','interviewed','assessed')),
  source          text NOT NULL CHECK (source IN ('email','upload')),
  file_path       text,
  photo_path      text,
  face_embedding  jsonb,
  completion_requests int NOT NULL DEFAULT 0 CHECK (completion_requests >= 0 AND completion_requests <= 2),
  completion_thread_id text,
  completion_token text,
  failure_reason  text,
  aggregate_score numeric(5,2) CHECK (aggregate_score BETWEEN 0 AND 100),
  received_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, job_id)
);
-- Existing databases created before identity / completeness fields.
ALTER TABLE candidate DROP CONSTRAINT IF EXISTS candidate_status_check;
ALTER TABLE candidate ADD CONSTRAINT candidate_status_check CHECK (status IN
  ('queued','processing','screened','failed','incomplete','interview_scheduled','interviewed','assessed'));
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS photo_path text;
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS face_embedding jsonb;
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS completion_requests int NOT NULL DEFAULT 0;
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS completion_thread_id text;
ALTER TABLE candidate ADD COLUMN IF NOT EXISTS completion_token text;

CREATE TABLE IF NOT EXISTS application (
  id              bigserial PRIMARY KEY,
  job_id          text REFERENCES job(id) ON DELETE CASCADE,   -- NULL when the email named no valid job
  candidate_id    text REFERENCES candidate(id) ON DELETE CASCADE,
  message_id_hash text NOT NULL UNIQUE,                     -- an email is never processed twice
  status          text NOT NULL CHECK (status IN ('accepted','rejected')),
  reject_reason   text,
  received_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (status = 'rejected' OR (job_id IS NOT NULL AND candidate_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS cv_parsed (
  candidate_id   text PRIMARY KEY REFERENCES candidate(id) ON DELETE CASCADE,
  parsed         jsonb NOT NULL,
  prompt_version text NOT NULL,
  parsed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS screening_result (
  candidate_id   text NOT NULL,
  job_id         text NOT NULL,
  requirement_id text NOT NULL,
  status         text NOT NULL CHECK (status IN ('met','partial','unmet')),
  justification  text NOT NULL,
  evidence_refs  jsonb NOT NULL DEFAULT '[]',
  prompt_version text NOT NULL,
  PRIMARY KEY (candidate_id, requirement_id),
  FOREIGN KEY (candidate_id, job_id)    REFERENCES candidate (id, job_id)       ON DELETE CASCADE,
  FOREIGN KEY (job_id, requirement_id)  REFERENCES job_requirement (job_id, req_id) ON DELETE CASCADE,
  CHECK (status = 'unmet' OR jsonb_array_length(evidence_refs) > 0)   -- met or partial must cite evidence
);

CREATE TABLE IF NOT EXISTS score (
  candidate_id   text NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
  dimension      text NOT NULL,
  value          numeric(5,2) NOT NULL CHECK (value BETWEEN 0 AND 100),
  weight         numeric(4,3) NOT NULL CHECK (weight BETWEEN 0 AND 1),
  justification  text NOT NULL,
  evidence_refs  jsonb NOT NULL DEFAULT '[]',
  prompt_version text NOT NULL,
  scored_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (candidate_id, dimension)
);

-- Same CV bytes and the same job rubric always reuse this judgment, so a repeat upload keeps the score.
CREATE TABLE IF NOT EXISTS screening_cache (
  content_hash text NOT NULL,
  rubric_key   text NOT NULL,
  result       jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (content_hash, rubric_key)
);

-- ===== Interviews ============================================================
CREATE TABLE IF NOT EXISTS interview_session (
  id                text PRIMARY KEY,
  job_id            text NOT NULL,
  candidate_id      text NOT NULL,
  status            text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','live','completed','interrupted')),
  join_token_hash   text NOT NULL UNIQUE,                   -- only a hash of the candidate link token is stored
  consent_at        timestamptz,
  consent_version   text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  scheduled_at      timestamptz,
  started_at        timestamptz,
  ended_at          timestamptz,
  calendar_uid      text,
  calendar_sequence int NOT NULL DEFAULT 0,
  transcript        jsonb,
  transcript_file   text,
  qa_result         jsonb,
  copilot_calls     int NOT NULL DEFAULT 0,
  copilot_times     jsonb NOT NULL DEFAULT '[]',
  eval_runs         int NOT NULL DEFAULT 0,
  FOREIGN KEY (candidate_id, job_id) REFERENCES candidate (id, job_id) ON DELETE CASCADE,
  UNIQUE (id, job_id),
  CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE IF NOT EXISTS transcript_turn (
  session_id text NOT NULL REFERENCES interview_session(id) ON DELETE CASCADE,
  turn_id    text NOT NULL,
  speaker    text NOT NULL CHECK (speaker IN ('interviewer','candidate')),
  start_ms   int  NOT NULL CHECK (start_ms >= 0),
  end_ms     int  NOT NULL,
  text       text NOT NULL,
  confidence real CHECK (confidence BETWEEN 0 AND 1),
  PRIMARY KEY (session_id, turn_id),
  CHECK (end_ms >= start_ms)
);

CREATE TABLE IF NOT EXISTS qa_pair (
  session_id        text NOT NULL REFERENCES interview_session(id) ON DELETE CASCADE,
  pair_id           text NOT NULL,
  question_turn_ids text[] NOT NULL CHECK (cardinality(question_turn_ids) > 0),
  answer_turn_ids   text[] NOT NULL CHECK (cardinality(answer_turn_ids) > 0),
  is_follow_up      boolean NOT NULL DEFAULT false,
  parent_pair_id    text,
  relevance         numeric(3,1) NOT NULL CHECK (relevance    BETWEEN 0 AND 10),
  accuracy          numeric(3,1) NOT NULL CHECK (accuracy     BETWEEN 0 AND 10),
  completeness      numeric(3,1) NOT NULL CHECK (completeness BETWEEN 0 AND 10),
  quality           numeric(3,1) NOT NULL CHECK (quality      BETWEEN 0 AND 10),
  justification     text NOT NULL,
  evidence_refs     jsonb NOT NULL CHECK (jsonb_array_length(evidence_refs) > 0),   -- every evaluation cites the transcript
  prompt_version    text NOT NULL,
  PRIMARY KEY (session_id, pair_id),
  FOREIGN KEY (session_id, parent_pair_id) REFERENCES qa_pair (session_id, pair_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS qa_pair_requirement (
  session_id     text NOT NULL,
  pair_id        text NOT NULL,
  job_id         text NOT NULL,
  requirement_id text NOT NULL,
  PRIMARY KEY (session_id, pair_id, requirement_id),
  FOREIGN KEY (session_id, pair_id)     REFERENCES qa_pair (session_id, pair_id)        ON DELETE CASCADE,
  FOREIGN KEY (session_id, job_id)      REFERENCES interview_session (id, job_id)        ON DELETE CASCADE,
  FOREIGN KEY (job_id, requirement_id)  REFERENCES job_requirement (job_id, req_id)      ON DELETE CASCADE
);

-- ===== Assessment ============================================================
CREATE TABLE IF NOT EXISTS assessment (
  id              text PRIMARY KEY,
  candidate_id    text NOT NULL UNIQUE REFERENCES candidate(id) ON DELETE CASCADE,
  session_id      text UNIQUE REFERENCES interview_session(id) ON DELETE CASCADE,
  status          text NOT NULL CHECK (status IN ('completed','partial')),
  has_interview   boolean NOT NULL,
  overall_position text NOT NULL CHECK (overall_position IN ('strong','promising','uncertain','weak')),
  cv_score        numeric(5,2) NOT NULL CHECK (cv_score BETWEEN 0 AND 100),
  interview_score numeric(5,2) CHECK (interview_score BETWEEN 0 AND 100),
  combined        numeric(5,2) NOT NULL CHECK (combined BETWEEN 0 AND 100),
  findings        jsonb NOT NULL,       -- strengths, weaknesses, evidence_gaps, follow_up_areas, each with evidence refs
  prompt_version  text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (has_interview = (session_id IS NOT NULL))
);

-- ===== Embeddings (RAG) ======================================================
-- One table for all vectors. Each row belongs to exactly one owner, so deleting the owner deletes its vectors.
-- Collections: 'policy' (owner: policy_document), 'cv:<candidate id>' (owner: candidate), 'req:<job id>' (owner: job).
CREATE TABLE IF NOT EXISTS vector_item (
  collection   text NOT NULL,
  item_id      text NOT NULL,
  policy_id    text REFERENCES policy_document(id) ON DELETE CASCADE,
  candidate_id text REFERENCES candidate(id)       ON DELETE CASCADE,
  job_id       text REFERENCES job(id)             ON DELETE CASCADE,
  text         text NOT NULL,
  embedding    vector(768) NOT NULL,
  PRIMARY KEY (collection, item_id),
  CHECK (num_nonnulls(policy_id, candidate_id, job_id) = 1)
);

-- ===== Recruiter accounts ====================================================
-- The password is a scrypt hash. Signup always creates an interviewer; admins stay in configuration.
CREATE TABLE IF NOT EXISTS recruiter (
  id            text PRIMARY KEY,
  email         text NOT NULL UNIQUE,
  name          text NOT NULL,
  password_hash text NOT NULL,
  role          text NOT NULL DEFAULT 'interviewer' CHECK (role IN ('interviewer', 'admin')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (email = lower(email)),
  CHECK (length(name) BETWEEN 1 AND 80),
  CHECK (length(password_hash) > 0),
  CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
);

-- ===== Audit log =============================================================
-- No foreign keys on purpose: the record must survive deletion of the thing it describes.
-- Holds IDs and outcomes only, never personal data.
CREATE TABLE IF NOT EXISTS audit_event (
  id         bigserial PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  actor_role text NOT NULL CHECK (actor_role IN ('interviewer','admin','candidate','system')),
  action     text NOT NULL,
  entity     text NOT NULL,
  entity_id  text NOT NULL,
  outcome    text NOT NULL CHECK (outcome IN ('ok','denied','error'))
);

-- ===== Indexes (foreign keys and common reads) ===============================
CREATE INDEX IF NOT EXISTS candidate_rank          ON candidate (job_id, aggregate_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS application_job         ON application (job_id);
CREATE INDEX IF NOT EXISTS screening_job_req       ON screening_result (job_id, requirement_id);
CREATE INDEX IF NOT EXISTS session_candidate       ON interview_session (candidate_id);
CREATE INDEX IF NOT EXISTS session_job             ON interview_session (job_id);
CREATE INDEX IF NOT EXISTS posting_decision_job    ON posting_decision (job_id);
CREATE INDEX IF NOT EXISTS publish_result_job      ON publish_result (job_id);
CREATE INDEX IF NOT EXISTS vector_policy           ON vector_item (policy_id)    WHERE policy_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS vector_candidate        ON vector_item (candidate_id) WHERE candidate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS vector_job              ON vector_item (job_id)       WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS audit_ts                ON audit_event (ts DESC);
CREATE INDEX IF NOT EXISTS audit_entity            ON audit_event (entity, entity_id);
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS scheduled_at timestamptz;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS calendar_uid text;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS calendar_sequence int NOT NULL DEFAULT 0;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS transcript jsonb;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS transcript_file text;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS qa_result jsonb;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS copilot_calls int NOT NULL DEFAULT 0;
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS copilot_times jsonb NOT NULL DEFAULT '[]';
ALTER TABLE interview_session ADD COLUMN IF NOT EXISTS eval_runs int NOT NULL DEFAULT 0;

-- One note per interview. Deleted with the candidate (N19).
CREATE TABLE IF NOT EXISTS interview_note (
  session_id   text PRIMARY KEY REFERENCES interview_session(id) ON DELETE CASCADE,
  candidate_id text NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
  author_id    text NOT NULL,
  author_name  text NOT NULL,
  body         text NOT NULL,
  markers      jsonb NOT NULL DEFAULT '[]',
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (char_length(body) <= 5000)
);

-- Global daily token count for the co-pilot. No personal data.
CREATE TABLE IF NOT EXISTS llm_budget (
  day    date PRIMARY KEY,
  tokens int NOT NULL DEFAULT 0 CHECK (tokens >= 0)
);
-- Optional, for large data: approximate vector search.
-- CREATE INDEX vector_item_hnsw ON vector_item USING hnsw (embedding vector_cosine_ops);
