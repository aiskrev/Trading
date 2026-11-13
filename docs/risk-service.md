# Risk service (ADR 0003, migration step 3)

The risk gateway and its signing key run in their own OS process. The orchestrator (live loop) reaches it
over mutual TLS on the loopback interface and holds no signing key. Status: **BACKTEST only**; approved as
APR-0009.

```
orchestrator process                            risk-service process
  live loop -> RemoteRiskGateway  --mTLS 1.3-->  RiskServiceServer -> RiskService -> RiskGateway + signer
  OrderVerifier (public key only) <--order----                        request log (hash-chained)
  SimulatedBroker                                                     kill-switch rule (layer 1)
```

## Protocol (`src/trading_agent/risk_service/protocol.py`)

| route | request | answer |
|---|---|---|
| `GET /v1/info` | none | `ServiceInfo`: service id, mode, account alias, policy version + SHA-256 + summary, key id, Ed25519 **public** key, order TTL |
| `POST /v1/evaluate` | `EvaluateRequest`: request id, proposal, critic verdict, market/account snapshots, trading state, features, broker view | `EvaluateResponse`: `RiskDecision` and at most one signed `ApprovedOrder` |

Every schema is closed (`extra="forbid"`): a request has no field for a mode, policy, threshold, account,
key or signature. JSON is parsed strictly (no duplicate keys, no NaN/Infinity, at most 512 KiB). Errors are a
`ServiceError` with a reason code only: `BAD_JSON`, `SCHEMA_FAILURE`, `BODY_TOO_LARGE`, `UNSUPPORTED_MEDIA_TYPE`,
`PEER_NOT_ALLOWED`, `NOT_FOUND`, `RATE_LIMITED`, `DUPLICATE_REQUEST`, `AUDIT_WRITE_FAILURE`, `INTERNAL_ERROR`.

## What the service enforces

1. **Mutual TLS 1.3**, both certificates from the configured CA. The client certificate must have EKU
   clientAuth and the common name `orchestrator`; the server certificate EKU serverAuth and DNS name
   `risk-service`. Failed handshakes are logged as `TLS_HANDSHAKE_FAILED`.
2. **Loopback only** (`127.0.0.1`, `::1`, `localhost`) on both sides until the host decision is made.
3. **Start-up gates:** the mode must be permitted (`PERMITTED_MODES_THIS_STAGE`) and the policy approved for
   it in `config/approvals.yaml`; otherwise the process does not start.
4. **Rate limit** (token bucket, 20/s, burst 40) and **request-id replay refusal**.
5. **Kill switch, layer 1:** the first gate rule; a switch file that exists, or whose state cannot be read,
   denies with `KILL_SWITCH_ACTIVE` / `KILL_SWITCH_UNREADABLE`.
6. **Request log** (`ChainedLog`, hash-chained): every request, answered or refused, with the body hash,
   proposal id, verdict, decision and order ids. Never a signature or key. If the record cannot be written,
   the order is discarded and `AUDIT_WRITE_FAILURE` is returned.
7. One request at a time; the TLS handshake runs under a 5 s timeout so a stalled peer cannot block it.

## What the client enforces (`client.py`)

* At start: `/v1/info` must answer with the expected mode and an approved policy hash, else `ConfigError`.
* On every fault (connection, TLS, timeout, non-200, bad JSON, schema failure) it returns a locally made
  **DENY** with a `RISK_SERVICE_*` reason code and no order. It never retries an evaluation.
* It checks every answer against its request: request id, proposal id, policy, requested notional; for an
  order, decision id, symbol, side, limit price, notional = approved notional, environment, account, key id,
  snapshot ids, issue time and lifetime. Any mismatch => DENY `RISK_SERVICE_INCONSISTENT_RESPONSE`.
* The execution side still verifies the signature and everything else (`OrderVerifier`).

## Running it (development)

```bash
python scripts/make_dev_certs.py ~/trading-agent-pki
python -m trading_agent.risk_service --pki-dir ~/trading-agent-pki --log ~/trading-agent-risk/requests.jsonl
```

Both directories must be outside the repository (both commands refuse otherwise). The dev CA's private key
is never written: to rotate, make a new directory and restart. The signing key is ephemeral (new per start,
so order ids carry a per-start service id and never repeat). In code, pass a `RemoteRiskGateway` as
`LiveLoopRunner(..., risk_gateway=...)`; the runner builds its `OrderVerifier` from the service's public key
and refuses a service running another mode or policy.

## Not in this step

* The signing key in the vault (step 4), leader epoch and the external kill-switch layers 2-3 (step 5).
* Option B (the service reads its own market and account data): today the orchestrator supplies the
  snapshots (option A), so a compromised orchestrator could still lie about prices or positions. The
  snapshots are content-hashed and bound into the signed order, so a lie is attributable afterwards.
* A networked deployment (host decision), certificate revocation, and the operator's own CA.
* The backtest engine keeps the in-process gateway (deterministic, no network).
