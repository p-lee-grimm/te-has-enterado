-- Очередь неразрешённых имён становится кликабельной.
--
-- Предложение приходит в Telegram, а инструкция в нём была консольная:
-- «запусти manage.py entity add». Владелец читает её с телефона и сделать
-- ничего не может, поэтому очередь копилась, а посты выходили с именами,
-- которых читатель не знает.
--
-- id          — короткий ключ для callback_data (64 байта на всё),
--               surface туда не влезает гарантированно;
-- surface_raw — исходное написание. В surface лежит нормализованная форма
--               («oscar puente»), из неё не восстановить «Óscar Puente»,
--               а именно оно идёт в name_es и в поиск по Википедии;
-- ignored_at  — «не нужно» должно держаться: без отметки имя вернётся
--               в очередь при следующем же упоминании.

ALTER TABLE entity_unresolved ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE entity_unresolved ADD COLUMN IF NOT EXISTS surface_raw TEXT;
ALTER TABLE entity_unresolved ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS entity_unresolved_id_key ON entity_unresolved (id);
