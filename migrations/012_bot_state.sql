-- Смещение getUpdates. Без него каждый прогон перечитывает всю очередь
-- обновлений заново и обрабатывает одни и те же нажатия по многу раз.
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
