-- Владелец источника. Три газеты одного холдинга с одной ленты — это одно
-- подтверждение, а не три, поэтому веткам допуска и рейтингу нужен владелец,
-- а не издание.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS owner_group TEXT;
UPDATE sources SET owner_group = id WHERE owner_group IS NULL;
