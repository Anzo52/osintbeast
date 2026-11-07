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

CREATE INDEX IF NOT EXISTS idx_toolcatalog_category
    ON ToolCatalog (category);
CREATE INDEX IF NOT EXISTS idx_toolsources_toolid
    ON ToolSources (toolID);
