-- Со звуком или беззвучно. Нужно, чтобы считать дневную квоту громких постов:
-- канал с двумя десятками уведомлений в день отписывают.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS with_sound BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS posts_sound_idx ON posts (published_at)
    WHERE with_sound;
