"""The risk service as its own process (ADR 0003, migration step 3).

The risk gateway and its signing key run in a separate OS process. The orchestrator talks to it over
mutual TLS on the loopback interface, through a narrow request/response schema (`protocol.py`):

  * `GET  /v1/info`      who the service is: mode, account alias, approved policy hash, public key;
  * `POST /v1/evaluate`  one proposal plus the snapshots it was made from -> a RiskDecision and, at most,
                         one signed ApprovedOrder.

Modules:
  protocol.py  wire schemas, limits and identities (no network code)
  pki.py       development certificates: a throwaway CA whose private key is never written to disk
  tls.py       TLS 1.3 contexts and peer identity
  service.py   the request logic: identity, rate limit, strict parsing, kill switch, gateway, request log
  server.py    the HTTPS listener (loopback only, one request at a time, handshake under a timeout)
  client.py    the orchestrator's side: same interface as RiskGateway.evaluate, fail closed on any fault
  __main__.py  composition root: `python -m trading_agent.risk_service --help`

The orchestrator never holds the signing key; every fault on the client side becomes a DENY.
"""
