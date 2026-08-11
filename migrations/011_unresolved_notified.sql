-- Когда об этой строке сообщили владельцу. Без отметки очередь либо молчит,
-- либо присылает одно и то же каждый прогон.
ALTER TABLE entity_unresolved ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
