-- Черновики смысловых правок (§2).
--
-- Ранние числа и формулировки в источниках меняются: количество пострадавших,
-- суммы, статус задержанных. Автоматически переписывать опубликованный текст
-- нельзя — цена ошибки выше цены задержки, — поэтому правка ждёт нажатия.
CREATE TABLE IF NOT EXISTS post_edits (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id      BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    new_header   TEXT NOT NULL,
    what_changed TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','applied','skipped')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS post_edits_pending_idx ON post_edits (post_id)
    WHERE status = 'pending';
