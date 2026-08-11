-- Продолжение сюжета — отдельный пост, а не правка старого.
--
-- Раньше на кластер приходилась ровно одна строка posts, и опубликованный
-- сюжет уже не мог выйти снова: значимое обновление было выразить нечем.
-- Теперь строк может быть несколько, а цепочка держится через
-- clusters.last_post_message_id — на него уходит реплай (§4.1).
ALTER TABLE posts DROP CONSTRAINT IF EXISTS posts_cluster_id_key;
CREATE INDEX IF NOT EXISTS posts_cluster_idx ON posts (cluster_id, published_at DESC NULLS FIRST);

-- сколько статей было в кластере на момент публикации: на этом стоит
-- правило повтора «добавилось >= N статей»
ALTER TABLE posts ADD COLUMN IF NOT EXISTS n_articles_at_publish INTEGER;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_continuation BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT;

ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_post_message_id BIGINT;

-- уже опубликованным проставим то, что знаем
UPDATE posts p SET n_articles_at_publish = c.n_articles
FROM clusters c WHERE c.id = p.cluster_id
  AND p.status = 'published' AND p.n_articles_at_publish IS NULL;
UPDATE clusters c SET last_post_message_id = p.message_id
FROM posts p WHERE p.cluster_id = c.id AND p.status = 'published'
  AND c.last_post_message_id IS NULL;
