-- url_canonical годится как ключ дедупа, но не как адрес для запроса:
-- у части изданий голый домен без www отдаёт битый TLS (elperiodico.com), а
-- у части ссылка живёт только в исходном виде. Поэтому храним оба:
--   url_canonical — тождество материала (дедуп),
--   url           — рабочий адрес (загрузка текста и ссылки в посте).
ALTER TABLE articles ADD COLUMN IF NOT EXISTS url TEXT;
UPDATE articles SET url = url_canonical WHERE url IS NULL;
