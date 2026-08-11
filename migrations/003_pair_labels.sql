-- Ручная разметка пар «одна история / разные» (§3.5).
-- Порог кластеризации калибруется глазами, но глаза стоит спрашивать один раз:
-- размеченные пары накапливаются и дают повторяемую оценку порога.
CREATE TABLE IF NOT EXISTS pair_labels (
    article_a   BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    article_b   BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    same_story  BOOLEAN NOT NULL,
    -- косинус на момент разметки: эмбеддинги не пересчитываются, но провайдер
    -- может смениться, и тогда старую разметку надо отбрасывать осознанно
    sim         REAL NOT NULL,
    labelled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (article_a, article_b),
    CONSTRAINT pair_labels_ordered CHECK (article_a < article_b)
);
