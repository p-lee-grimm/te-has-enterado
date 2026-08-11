-- Ссылки на материалы, где встретилось имя. Без них предложение завести
-- сущность невозможно оценить: чтобы решить, кто такой Miguel Ángel Galán,
-- надо сначала прочитать новость, в которой он появился.
ALTER TABLE entity_unresolved ADD COLUMN IF NOT EXISTS sample_urls JSONB NOT NULL DEFAULT '[]'::jsonb;
