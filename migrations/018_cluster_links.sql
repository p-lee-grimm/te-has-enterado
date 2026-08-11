-- Связи между разными сюжетами (§4.2).
--
-- «Центры репатриации в Италии» и «ситуация в Сеуте» — соседи, а не
-- продолжение. Реплаем это выражать нельзя: реплай означает продолжение.
--
-- Отбор настроен на точность, не на полноту: одна нелепая связка
-- дискредитирует механику сильнее, чем десять пропущенных связей помогают.
-- Поэтому есть ручной blocked — пара, отклонённая владельцем, не предлагается
-- больше никогда.
CREATE TABLE IF NOT EXISTS cluster_links (
    cluster_a  BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    cluster_b  BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('related', 'blocked')),
    created_by TEXT NOT NULL DEFAULT 'auto',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_a, cluster_b),
    -- пара хранится в одном порядке, иначе blocked ставится дважды
    CONSTRAINT cluster_links_ordered CHECK (cluster_a < cluster_b)
);
