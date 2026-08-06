"""HTCondor IDTOKEN issuance for the UChicago ATLAS Analysis Facility MCP platform.

Verifies broker-minted AF Broker Identity Tokens (RS256 JWTs) and mints
HTCondor IDTOKENS via ``condor_token_create``. The HTCondor pool password
(the symmetric IDTOKEN signing key) never leaves Condor infrastructure —
this service runs where that key lives so the broker never has to hold it.
"""
