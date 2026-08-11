-- Замерный режим (§10).
--
-- Раскрывающаяся цитата не даёт статистики: раскрытие никак не считается.
-- Чтобы проверить главную гипотезу проекта — что разрыв контекстный,
-- а не языковой, — раз в неделю карточки выходят на инлайн-кнопках,
-- и каждый тап записывается.
CREATE TABLE IF NOT EXISTS context_taps (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    post_id   BIGINT REFERENCES posts(id) ON DELETE SET NULL,
    user_id   BIGINT,
    tapped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS context_taps_post_idx ON context_taps (post_id);
