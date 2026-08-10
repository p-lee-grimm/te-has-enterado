-- Схема v0. Идемпотентна: можно применять повторно.
-- Применение: python run.py --migrate

CREATE EXTENSION IF NOT EXISTS vector;

-- Размерность эмбеддинга должна совпадать с embed.dimensions в settings.yaml.
-- Меняется только вместе с пересозданием таблицы articles.

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    feed_url      TEXT NOT NULL DEFAULT '',
    type          TEXT NOT NULL CHECK (type IN ('press', 'agency', 'official')),
    lean          TEXT NOT NULL CHECK (lean IN ('left','center-left','center','center-right','right')),
    weight        REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    etag          TEXT,
    last_modified TEXT,
    last_fetch_at TIMESTAMPTZ,
    last_ok_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS clusters (
    id                        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    centroid                  vector({{EMBED_DIM}}),
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    n_articles                INTEGER NOT NULL DEFAULT 0,
    n_sources                 INTEGER NOT NULL DEFAULT 0,
    score                     REAL NOT NULL DEFAULT 0,
    last_published_digest_id  BIGINT,
    last_published_at         TIMESTAMPTZ,
    -- сколько статей было в кластере на момент последней публикации:
    -- нужно для правила повтора (§3.7), иначе «≥3 новых статьи» не посчитать
    n_articles_at_publish     INTEGER
);

CREATE TABLE IF NOT EXISTS articles (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url_canonical   TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary_feed    TEXT,
    body            TEXT,
    body_expires_at TIMESTAMPTZ,
    title_hash      TEXT NOT NULL,
    published_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding       vector({{EMBED_DIM}}),
    cluster_id      BIGINT REFERENCES clusters(id) ON DELETE SET NULL,
    CONSTRAINT articles_url_canonical_key UNIQUE (url_canonical)
);

CREATE TABLE IF NOT EXISTS digests (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','pending_review','published','skipped','blocked')),
    telegram_message_id BIGINT,
    item_count          INTEGER NOT NULL DEFAULT 0,
    gate_report         JSONB,
    body_html           TEXT
);

CREATE TABLE IF NOT EXISTS digest_items (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_id       BIGINT NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
    cluster_id      BIGINT REFERENCES clusters(id) ON DELETE SET NULL,
    position        INTEGER NOT NULL,
    headline        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    context         TEXT NOT NULL DEFAULT '',
    framing         TEXT NOT NULL DEFAULT '',
    topic           TEXT NOT NULL DEFAULT '',
    confidence      TEXT NOT NULL DEFAULT 'high' CHECK (confidence IN ('high','low')),
    is_continuation BOOLEAN NOT NULL DEFAULT FALSE,
    links           JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    stage_stats    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','ok','failed','gated')),
    error          TEXT,
    llm_tokens_in  BIGINT NOT NULL DEFAULT 0,
    llm_tokens_out BIGINT NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(10, 4) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS articles_published_at_idx  ON articles (published_at DESC);
CREATE INDEX IF NOT EXISTS articles_cluster_id_idx    ON articles (cluster_id);
CREATE INDEX IF NOT EXISTS articles_fetched_at_idx    ON articles (fetched_at DESC);
CREATE INDEX IF NOT EXISTS articles_body_expires_idx  ON articles (body_expires_at)
    WHERE body IS NOT NULL;
-- дедуп по заголовку внутри одного источника (§3.3)
CREATE INDEX IF NOT EXISTS articles_source_title_idx  ON articles (source_id, title_hash);
CREATE INDEX IF NOT EXISTS clusters_open_idx          ON clusters (last_seen_at DESC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS digest_items_digest_idx    ON digest_items (digest_id, position);
CREATE INDEX IF NOT EXISTS runs_started_idx           ON runs (started_at DESC);

-- ivfflat строится только когда в таблице уже есть данные — иначе список центроидов
-- получается вырожденным. Создаётся отдельно: python run.py --build-vector-index
