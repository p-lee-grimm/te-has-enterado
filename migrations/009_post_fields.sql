-- Поля поста из §6 спеки контекстного слоя.
--   significance — одна строка «зачем это читателю», всегда в теле поста;
--   one_sided    — сюжет прошёл только по ветке «≥N владельцев», и все
--                  источники в одном бакете; в посте это отдельная строка.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS significance TEXT NOT NULL DEFAULT '';
ALTER TABLE posts ADD COLUMN IF NOT EXISTS one_sided BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS scope TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS geo_tag TEXT;
