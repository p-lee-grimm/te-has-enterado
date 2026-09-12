-- Жанр материала: публиковать ли сюжет вообще.
--
-- scope отвечает на вопрос «про кого новость», impact — на вопрос
-- «меняет ли она что-нибудь для читателя». Это разные оси: свидание
-- в Мадриде — spain + soft, похороны в Осло — world + soft.
--
-- Штраф в формуле ранжирования тут не работает: светская хроника собирает
-- максимальный кросс-спектральный охват (её печатают все издания независимо
-- от политики), поэтому метрика широты консенсуса поднимает её наверх,
-- а не вниз. Нужен отдельный классификатор до отбора.
--
-- Храним на сюжете по той же причине, что и topic: без колонки отклонённый
-- сюжет прогонялся бы через модель каждые полчаса заново.
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS impact TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS impact_reason TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS impact_confidence TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS impact_at TIMESTAMPTZ;

-- Раздел издания, откуда пришёл материал: gente, deportes, economia.
-- Виден в URL и в категориях RSS. Нужен и фильтру на этапе fetch,
-- и классификатору как признак.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS section TEXT;
