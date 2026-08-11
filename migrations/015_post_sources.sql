-- Что уже прилинковано в посте. Отдельная таблица, а не разбор текста
-- регуляркой: текст правит человек, и парсить его обратно — самый быстрый
-- способ однажды затереть ручную правку.
CREATE TABLE IF NOT EXISTS post_sources (
    post_id     BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    article_id  BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    owner_group TEXT NOT NULL DEFAULT '',
    bucket      TEXT NOT NULL DEFAULT '',
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, article_id)
);
CREATE INDEX IF NOT EXISTS post_sources_post_idx ON post_sources (post_id);
