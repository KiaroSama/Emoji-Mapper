-- Log lines from both bots, held under a byte budget by src/logging.ts.
--
-- `bytes` stores the size of this row's text so eviction can sum newest-first
-- without re-measuring every row on every write. It is written by the inserter,
-- never computed here: SQLite's length() counts characters, not bytes, and a
-- Persian or emoji-bearing detail would be undercounted.
CREATE TABLE IF NOT EXISTS logs (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,          -- epoch milliseconds, UTC
  bot    TEXT    NOT NULL,          -- 'general' | 'coin'
  level  TEXT    NOT NULL,          -- INFO | WARNING | ERROR
  event  TEXT    NOT NULL,          -- short stable key, e.g. 'webhook'
  detail TEXT,                      -- free text, already redacted
  bytes  INTEGER NOT NULL           -- byte length of bot+level+event+detail
);

-- AUTOINCREMENT, not a plain rowid alias: eviction deletes the oldest rows and
-- rowid reuse after a delete would let a new row land BELOW an existing one,
-- breaking the "id is monotonic, so id <= X means older" assumption the
-- newest-first running sum depends on.

-- Reading the log is always "the newest N", optionally for one bot.
CREATE INDEX IF NOT EXISTS logs_ts_idx  ON logs (ts DESC);
CREATE INDEX IF NOT EXISTS logs_bot_idx ON logs (bot, id DESC);
