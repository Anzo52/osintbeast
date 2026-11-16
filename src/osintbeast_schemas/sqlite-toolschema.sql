-- Tool metadata and results
CREATE TABLE IF NOT EXISTS ToolCatalog (
    toolID INTEGER PRIMARY KEY,
    toolName TEXT NOT NULL UNIQUE,
    category TEXT,
    repositoryURL TEXT,
    installMethod TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS ToolResults (
    resultID INTEGER PRIMARY KEY AUTOINCREMENT,
    toolName TEXT NOT NULL,
    queryText TEXT NOT NULL,
    resultText TEXT,
    createdAt TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_toolresults_tool_created
    ON ToolResults (toolName, createdAt);

CREATE TABLE IF NOT EXISTS ToolRuns (
    runID INTEGER PRIMARY KEY AUTOINCREMENT,
    toolName TEXT NOT NULL,
    queryText TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'timed_out')),
    resultText TEXT,
    errorText TEXT,
    startedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finishedAt TEXT
);

CREATE INDEX IF NOT EXISTS idx_toolruns_tool_started
    ON ToolRuns (toolName, startedAt);
