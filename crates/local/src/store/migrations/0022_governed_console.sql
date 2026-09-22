CREATE TABLE governed_project_requests (
 actor TEXT NOT NULL REFERENCES identity_principals(id),
 request_key TEXT NOT NULL, name TEXT NOT NULL, directory TEXT NOT NULL UNIQUE,
 deployment TEXT REFERENCES deployments(id), branch TEXT REFERENCES branches(id),
 PRIMARY KEY(actor,request_key)
);
