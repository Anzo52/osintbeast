CREATE TABLE IF NOT EXISTS ToolCatalog (
    toolID INT NOT NULL AUTO_INCREMENT,
    toolName VARCHAR(255) NOT NULL,
    category VARCHAR(128) DEFAULT NULL,
    repositoryURL VARCHAR(2048) DEFAULT NULL,
    installMethod VARCHAR(64) DEFAULT NULL,
    notes TEXT,
    PRIMARY KEY (toolID),
    UNIQUE KEY uq_toolcatalog_name (toolName),
    KEY idx_toolcatalog_category (category)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ToolSources (
    sourceID INT NOT NULL AUTO_INCREMENT,
    toolID INT NOT NULL,
    sourceName VARCHAR(255) NOT NULL,
    sourceType VARCHAR(64) NOT NULL,
    sourceURL VARCHAR(2048) DEFAULT NULL,
    PRIMARY KEY (sourceID),
    KEY idx_toolsources_toolid (toolID),
    CONSTRAINT fk_toolsources_toolcatalog FOREIGN KEY (toolID) REFERENCES ToolCatalog (toolID)
) ENGINE=InnoDB;
