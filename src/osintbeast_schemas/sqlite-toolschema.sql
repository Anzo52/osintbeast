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
