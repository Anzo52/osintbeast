-- Tool metadata and results
CREATE TABLE IF NOT EXISTS ToolCatalog (
    toolID INTEGER PRIMARY KEY,
    toolName TEXT NOT NULL UNIQUE,
    category TEXT,
    repositoryURL TEXT,
    installMethod TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS ToolSources (
    sourceID INTEGER PRIMARY KEY,
    toolID INTEGER NOT NULL,
    sourceName TEXT NOT NULL,
    sourceType TEXT NOT NULL,
    sourceURL TEXT,
    FOREIGN KEY (toolID) REFERENCES ToolCatalog (toolID)
);

CREATE TABLE IF NOT EXISTS ToolResults (
    resultID INTEGER PRIMARY KEY,
    toolName TEXT NOT NULL,
    queryText TEXT NOT NULL,
    resultText TEXT,
    createdAt TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_toolcatalog_category
    ON ToolCatalog (category);
CREATE INDEX IF NOT EXISTS idx_toolsources_toolid
    ON ToolSources (toolID);
CREATE INDEX IF NOT EXISTS idx_toolresults_tool_created
    ON ToolResults (toolName, createdAt);

CREATE TABLE IF NOT EXISTS ToolRuns (
    runID INTEGER PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS ToolRunRequests (
    idempotencyKey TEXT PRIMARY KEY,
    requestHash TEXT NOT NULL,
    runID INTEGER NOT NULL UNIQUE,
    FOREIGN KEY (runID) REFERENCES ToolRuns (runID) ON DELETE CASCADE
);
