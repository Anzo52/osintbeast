CREATE TABLE Platforms (
        platformID integer primary key,
        platform text unique not null
);
CREATE TABLE Names (
        nameID integer primary key,
        firstName text not null,
        middleName text,
        lastName text,
        other text
);
CREATE TABLE Locations (
        locationID integer primary key, 
        country text,
        state text,
        county text,
        city text,
        street text,
        addressNum integer,
        apt integer,
        zip integer 
);
CREATE TABLE Domains (
        domainID int primary key,
        domain text not null unique
);
CREATE TABLE Services (
        serviceID integer primary key,
        service text not null,
        domainID integer,
        platformID integer,
        foreign key (domainID) references Domains (domainID),
        foreign key (platformID) references Platforms (platformID)
);
CREATE TABLE Urls (
        urlID integer primary key,
        url text not null,
        domainID integer,
        foreign key (domainID) references Domains (domainID)
);
CREATE TABLE Usernames (
        usernameID integer primary key,
        username text not null unique,
        nameID integer,
        serviceID integer,
        foreign key (nameID) references Names (nameID),
        foreign key (serviceID) references Services (serviceID) 
);
