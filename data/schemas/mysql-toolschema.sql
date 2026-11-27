CREATE TABLE IF NOT EXISTS ToolCatalog (
    toolID INT NOT NULL AUTO_INCREMENT,
    toolName VARCHAR(255) NOT NULL,
    category VARCHAR(255) DEFAULT NULL,
    repositoryURL VARCHAR(2048) DEFAULT NULL,
    installMethod VARCHAR(255) DEFAULT NULL,
    notes TEXT,
    PRIMARY KEY (toolID),
    UNIQUE KEY uq_toolcatalog_toolname (toolName),
    KEY idx_toolcatalog_category (category)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolSources (
    sourceID INT NOT NULL AUTO_INCREMENT,
    toolID INT NOT NULL,
    sourceName VARCHAR(255) NOT NULL,
    sourceType VARCHAR(255) NOT NULL,
    sourceURL VARCHAR(2048) DEFAULT NULL,
    PRIMARY KEY (sourceID),
    KEY idx_toolsources_toolid (toolID),
    CONSTRAINT fk_toolsources_toolcatalog
        FOREIGN KEY (toolID) REFERENCES ToolCatalog (toolID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolResults (
    resultID INT NOT NULL AUTO_INCREMENT,
    toolName VARCHAR(255) NOT NULL,
    queryText TEXT NOT NULL,
    resultText LONGTEXT,
    createdAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (resultID),
    KEY idx_toolresults_tool_created (toolName, createdAt)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolRuns (
    runID INT NOT NULL AUTO_INCREMENT,
    toolName VARCHAR(255) NOT NULL,
    queryText TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    resultText LONGTEXT,
    errorText TEXT,
    startedAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finishedAt DATETIME DEFAULT NULL,
    PRIMARY KEY (runID),
    KEY idx_toolruns_tool_started (toolName, startedAt),
    CONSTRAINT chk_toolruns_status
        CHECK (status IN ('running', 'success', 'failed', 'timed_out'))
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolRunIdempotency (
    keyHash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    toolName VARCHAR(255) NOT NULL,
    inputFingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    runID INT NOT NULL,
    PRIMARY KEY (keyHash),
    UNIQUE KEY uq_toolrun_idempotency_run (runID),
    CONSTRAINT fk_toolrun_idempotency_run
        FOREIGN KEY (runID) REFERENCES ToolRuns (runID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolRunExecutions (
    runID INT NOT NULL,
    state VARCHAR(16) NOT NULL,
    attempt INT NOT NULL DEFAULT 0,
    ownerToken CHAR(32) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    leaseExpiresAt BIGINT DEFAULT NULL,
    PRIMARY KEY (runID),
    KEY idx_toolrunexecutions_recovery (state, leaseExpiresAt),
    CONSTRAINT fk_toolrunexecutions_toolrun
        FOREIGN KEY (runID) REFERENCES ToolRuns (runID),
    CONSTRAINT chk_toolrunexecutions_state
        CHECK (
            state IN (
                'queued', 'running', 'success', 'failed', 'timed_out',
                'cancelled', 'indeterminate'
            )
        )
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolRunRequests (
    idempotencyKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    requestHash CHAR(64) NOT NULL,
    runID INT NOT NULL,
    PRIMARY KEY (idempotencyKey),
    UNIQUE KEY uq_toolrunrequests_runid (runID),
    CONSTRAINT fk_toolrunrequests_toolrun
        FOREIGN KEY (runID) REFERENCES ToolRuns (runID)
) ENGINE=InnoDB;
