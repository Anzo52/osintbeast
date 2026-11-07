CREATE TABLE IF NOT EXISTS Platforms (
    platformID INT NOT NULL AUTO_INCREMENT,
    platform VARCHAR(255) NOT NULL,
    PRIMARY KEY (platformID),
    UNIQUE KEY uq_platforms_platform (platform)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Names (
    nameID INT NOT NULL AUTO_INCREMENT,
    firstName VARCHAR(255) NOT NULL,
    middleName VARCHAR(255) DEFAULT NULL,
    lastName VARCHAR(255) DEFAULT NULL,
    other TEXT,
    PRIMARY KEY (nameID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Locations (
    locationID INT NOT NULL AUTO_INCREMENT,
    country VARCHAR(255) DEFAULT NULL,
    usState VARCHAR(255) DEFAULT NULL,
    county VARCHAR(255) DEFAULT NULL,
    city VARCHAR(255) DEFAULT NULL,
    street VARCHAR(255) DEFAULT NULL,
    addressNum INT DEFAULT NULL,
    apt INT DEFAULT NULL,
    zip INT DEFAULT NULL,
    PRIMARY KEY (locationID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Domains (
    domainID INT NOT NULL AUTO_INCREMENT,
    domain VARCHAR(255) NOT NULL,
    PRIMARY KEY (domainID),
    UNIQUE KEY uq_domains_domain (domain)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Services (
    serviceID INT NOT NULL AUTO_INCREMENT,
    serviceName VARCHAR(255) NOT NULL,
    domainID INT DEFAULT NULL,
    platformID INT DEFAULT NULL,
    PRIMARY KEY (serviceID),
    KEY idx_services_domain (domainID),
    KEY idx_services_platform (platformID),
    CONSTRAINT fk_services_domains FOREIGN KEY (domainID) REFERENCES Domains (domainID),
    CONSTRAINT fk_services_platforms FOREIGN KEY (platformID) REFERENCES Platforms (platformID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Urls (
    urlID INT NOT NULL AUTO_INCREMENT,
    urlAddress VARCHAR(2048) NOT NULL,
    domainID INT DEFAULT NULL,
    PRIMARY KEY (urlID),
    KEY idx_urls_domain (domainID),
    CONSTRAINT fk_urls_domains FOREIGN KEY (domainID) REFERENCES Domains (domainID)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS Usernames (
    usernameID INT NOT NULL AUTO_INCREMENT,
    username VARCHAR(255) NOT NULL,
    nameID INT DEFAULT NULL,
    serviceID INT DEFAULT NULL,
    PRIMARY KEY (usernameID),
    UNIQUE KEY uq_usernames_username (username),
    KEY idx_usernames_name (nameID),
    KEY idx_usernames_service (serviceID),
    CONSTRAINT fk_usernames_names FOREIGN KEY (nameID) REFERENCES Names (nameID),
    CONSTRAINT fk_usernames_services FOREIGN KEY (serviceID) REFERENCES Services (serviceID)
) ENGINE=InnoDB;
