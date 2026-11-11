-- OSINT records
CREATE TABLE IF NOT EXISTS Platforms (
    platformID INTEGER PRIMARY KEY,
    platform TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS Names (
    nameID INTEGER PRIMARY KEY,
    firstName TEXT NOT NULL,
    middleName TEXT,
    lastName TEXT,
    other TEXT
);

CREATE TABLE IF NOT EXISTS Locations (
    locationID INTEGER PRIMARY KEY,
    country TEXT,
    usState TEXT,
    county TEXT,
    city TEXT,
    street TEXT,
    addressNum INTEGER,
    apt INTEGER,
    zip INTEGER
);

CREATE TABLE IF NOT EXISTS Domains (
    domainID INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Services (
    serviceID INTEGER PRIMARY KEY,
    serviceName TEXT NOT NULL,
    domainID INTEGER,
    platformID INTEGER,
    FOREIGN KEY (domainID) REFERENCES Domains (domainID),
    FOREIGN KEY (platformID) REFERENCES Platforms (platformID)
);

CREATE TABLE IF NOT EXISTS Urls (
    urlID INTEGER PRIMARY KEY,
    urlAddress TEXT NOT NULL,
    domainID INTEGER,
    FOREIGN KEY (domainID) REFERENCES Domains (domainID)
);

CREATE TABLE IF NOT EXISTS Usernames (
    usernameID INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    nameID INTEGER,
    serviceID INTEGER,
    FOREIGN KEY (nameID) REFERENCES Names (nameID),
    FOREIGN KEY (serviceID) REFERENCES Services (serviceID)
);

CREATE INDEX IF NOT EXISTS idx_services_domain_platform
    ON Services (domainID, platformID);
CREATE INDEX IF NOT EXISTS idx_urls_domain
    ON Urls (domainID);
CREATE INDEX IF NOT EXISTS idx_usernames_service
    ON Usernames (serviceID);
CREATE INDEX IF NOT EXISTS idx_usernames_name
    ON Usernames (nameID);
