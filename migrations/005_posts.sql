-- Пост на сюжет вместо одного дайджеста в сутки.
--
-- Издания пишут об одном событии не одновременно, поэтому пост живёт: сначала
-- публикуется с теми источниками, что уже есть, потом дополняется по мере того,
-- как подтягиваются остальные. Правим сообщение, а не постим второе.

-- Спектр расширяется до крайних флангов. Значение проставляет владелец руками,
-- автоклассификации нет и не будет (§2).
ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_lean_check;
ALTER TABLE sources ADD CONSTRAINT sources_lean_check CHECK (lean IN (
    'far-left', 'left', 'center-left', 'center', 'center-right', 'right', 'far-right'
));

CREATE TABLE IF NOT EXISTS posts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id    BIGINT NOT NULL UNIQUE REFERENCES clusters(id) ON DELETE CASCADE,

    -- то, что писал человек: заголовок и (необязательно) пояснение, в markdown.
    -- Блок ссылок и хэштег к этому не относятся — они собираются заново при
    -- каждой правке, иначе дополнение поста затирало бы ручной текст.
    header_md     TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',

    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'published', 'skipped')),
    message_id    BIGINT,
    -- источники, которые уже попали в опубликованный пост: по ним понимаем,
    -- что появилось нового и нужна ли правка
    posted_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ,
    edited_at     TIMESTAMPTZ,
    edit_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS posts_status_idx ON posts (status, published_at DESC);
