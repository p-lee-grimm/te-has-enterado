-- Контекстный слой: карточки сущностей (§7).
--
-- Читатель без испанского бэкграунда спотыкается об имена: кто такой
-- Florentino Pérez, что такое Junts. Карточка отвечает ТОЛЬКО на «кто это»;
-- почему новость важна — это significance в теле поста, потому что одна и та
-- же сущность в разных сюжетах важна по-разному.

CREATE TABLE IF NOT EXISTS entities (
    id                TEXT PRIMARY KEY,          -- slug: florentino-perez
    type              TEXT NOT NULL DEFAULT 'other'
                      CHECK (type IN ('person','company','party','institution','media','other')),
    name_es           TEXT NOT NULL,             -- как пишем в посте
    name_ru           TEXT NOT NULL DEFAULT '',  -- для матчинга, не для показа
    card              TEXT NOT NULL DEFAULT '',  -- <= 200 символов
    card_status       TEXT NOT NULL DEFAULT 'draft'
                      CHECK (card_status IN ('draft','approved','stale')),
    wiki_url_es       TEXT,
    wiki_url_ru       TEXT,
    -- имена, которые аудитория заведомо знает, объяснять не надо
    never_explain     BOOLEAN NOT NULL DEFAULT FALSE,
    mentions_count    INTEGER NOT NULL DEFAULT 0,
    last_explained_at TIMESTAMPTZ,
    card_updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,                     -- хранится нормализованным
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS entity_aliases_alias_idx ON entity_aliases (alias);

CREATE TABLE IF NOT EXISTS entity_mentions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    cluster_id BIGINT REFERENCES clusters(id) ON DELETE CASCADE,
    post_id    BIGINT REFERENCES posts(id) ON DELETE SET NULL,
    -- упоминание без показа тоже пишем: по этим данным видно,
    -- какие сущности покрывают повестку
    shown      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Не служебная таблица, а рабочая очередь: раз в неделю владелец смотрит
-- топ по count и решает, заводить сущность или добавить алиас.
CREATE TABLE IF NOT EXISTS entity_unresolved (
    surface        TEXT PRIMARY KEY,
    count          INTEGER NOT NULL DEFAULT 1,
    candidate_id   TEXT,                          -- на кого похоже, если похоже
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sample_context TEXT
);

ALTER TABLE posts ADD COLUMN IF NOT EXISTS entity_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
