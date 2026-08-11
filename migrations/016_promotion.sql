-- Окно повышения и утренний пересмотр (§2).
--
-- Кластер, не набравший источников сразу, остаётся кандидатом ограниченное
-- время: испанский цикл устроен так, что вечернее заявление получает ответ
-- оппонирующих изданий только в утренних выпусках. Без пересмотра целый класс
-- сюжетов структурно не может набрать кросс-подтверждение — то есть именно то,
-- ради чего заведена шкала.
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;
-- scope определяется вызовом LLM; храним, чтобы вечерний пост знал про world,
-- не спрашивая модель заново
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS scope TEXT;
CREATE INDEX IF NOT EXISTS clusters_expired_idx ON clusters (expired_at)
    WHERE expired_at IS NULL;
