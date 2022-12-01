# Development notes

## Thoughts

The ultimate purpose of the tool is to provide automation.

----------------

## DB schema

### info/target sqlite3 schema

tables|fields|keys/indices
---|---|---
Platforms | platformID, platform | platformID
Names | nameID, firstName, middleName, lastName, other | nameID
Locations | locationID, country, usState, county, city, street, addressNum, apt, zip | locationID
Domains | domainID, domain | domainID
Services | serviceID, serviceName, domainID, platformID | serviceID, domainID, platformID
Urls | urlID, urlAddress, domainID | urlID, domainID
Usernames | usernameID, username, nameID, serviceID | usernameID, nameID, serviceID

### info/target mysql schema

#### tools sqlite3 schema

Not sure what would be best here especially concerning indexing.
Ideally I would like the user to be able to find a tool based on:
    a) the information they are seeking
    b) the information they have, or
    c) combining both to build a full profile of target.

#### tools mysql schema

----------------

## Tools

### Tools to add

### Tools added

### (bonus) Tools to fix before adding

----------------