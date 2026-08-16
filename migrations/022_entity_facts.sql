-- Пул фактов сущностей вместо карточки-строки.
--
-- Карточка была одним текстом на все посты, и это ломалось о два требования.
-- Первое — релевантность: Florentino Pérez в новости про долю в газете и он же
-- в новости про стадион требуют разных фактов из одного набора. Второе —
-- опора на источник в каждом утверждении, включая оценочные: проверять всю
-- строку целиком дорого и приходится заново при каждой правке.
--
-- Поэтому хранится не строка, а пул атомарных фактов. Проверка факта дорогая
-- и разовая: факт несёт дословную цитату из источника и живёт годами. Сборка
-- контекста дешёвая и повторяется на каждый пост: из пула выбираются два
-- факта, ближайших к теме сюжета. Сборщик не может внести новое утверждение
-- по построению — он только выбирает и соединяет уже проверенное.

CREATE TABLE IF NOT EXISTS entity_facts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    -- одно проверяемое утверждение; предложение с двумя фактами разбивается
    fact        TEXT NOT NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN ('role','scale','classification','evaluative','legal')),
    -- к каким темам сюжета факт относится: по ним сборщик и выбирает
    topics      TEXT[] NOT NULL DEFAULT '{}',
    source_url  TEXT,
    source_tier TEXT NOT NULL DEFAULT 'wiki'
                CHECK (source_tier IN ('wiki','wiki_org','wikidata','official',
                                       'press','legacy')),
    -- полюс источника: спорность решается тем, сходятся ли разные бакеты,
    -- а не тем, что модель считает спорным
    source_bucket TEXT NOT NULL DEFAULT '',
    -- дословный фрагмент источника; ищется в тексте скриптом, а не критиком
    quote       TEXT NOT NULL DEFAULT '',
    -- название издания; для kind=evaluative обязательно и внутри самого факта
    attribution TEXT NOT NULL DEFAULT '',
    verified_at TIMESTAMPTZ,
    -- NULL для бессрочных; у kind=legal — verified_at + facts.legal_ttl_days
    expires_at  TIMESTAMPTZ,
    -- candidate — характеристика от одного полюса: в пул не идёт, пока
    -- не появится источник с другой стороны (§4 спеки)
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','candidate','stale','retired')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_facts_entity_idx
    ON entity_facts (entity_id, status);
-- Один и тот же факт из повторного извлечения не должен множиться в пуле.
CREATE UNIQUE INDEX IF NOT EXISTS entity_facts_uniq
    ON entity_facts (entity_id, lower(btrim(fact)));

-- Собранный контекст хранится на посте, а не считается заново при каждой
-- правке: пост дополняется ссылками десятки раз, и звать модель на каждую
-- правку значило бы платить за неё десятки раз и получать каждый раз другой
-- текст под тем же сообщением.
-- Вид: {"entity-id": {"context": "…", "fact_ids": [12, 45]}}
ALTER TABLE posts ADD COLUMN IF NOT EXISTS entity_context JSONB NOT NULL
    DEFAULT '{}'::jsonb;

-- Когда пул сущности пересобирался в последний раз.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS facts_updated_at TIMESTAMPTZ;

-- Сайт госоргана, парламента, партии или издания. Проставляется руками:
-- самоописание годится для фактического каркаса (должность, отрасль) и
-- никогда для характеристики — ни одна партия не назовёт себя ультраправой.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS official_url TEXT;

-- Статья Википедии об организации, если своей у персоны нет: роль тогда
-- записывается фактом со ссылкой на статью организации (§5 спеки).
ALTER TABLE entities ADD COLUMN IF NOT EXISTS wiki_url_org TEXT;

-- Старые карточки не выбрасываем, но и не показываем: у них нет цитаты,
-- а факт без цитаты непроверяем — это ровно то, ради чего всё менялось.
-- Они ложатся в пул со статусом stale и ждут переизвлечения
-- (`python manage.py fact extract --all --commit`), а до тех пор читателя
-- держит role_gloss в теле поста.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'entities' AND column_name = 'card') THEN
        INSERT INTO entity_facts (entity_id, fact, kind, source_url,
                                  source_tier, status)
        SELECT id, btrim(card), 'role',
               COALESCE(NULLIF(wiki_url_ru, ''), NULLIF(wiki_url_es, '')),
               'legacy', 'stale'
        FROM entities
        WHERE btrim(COALESCE(card, '')) <> ''
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

ALTER TABLE entities DROP COLUMN IF EXISTS card;
ALTER TABLE entities DROP COLUMN IF EXISTS card_status;
ALTER TABLE entities DROP COLUMN IF EXISTS card_updated_at;
