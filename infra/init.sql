CREATE ROLE policyproof_api LOGIN PASSWORD 'api-dev-only';
CREATE ROLE policyproof_target LOGIN PASSWORD 'target-dev-only';
CREATE ROLE policyproof_oracle LOGIN PASSWORD 'oracle-dev-only';

GRANT CONNECT ON DATABASE policyproof TO policyproof_api;
GRANT CONNECT ON DATABASE policyproof TO policyproof_target;
GRANT CONNECT ON DATABASE policyproof TO policyproof_oracle;
GRANT CREATE ON DATABASE policyproof TO policyproof_api;
GRANT CREATE ON DATABASE policyproof TO policyproof_target;
