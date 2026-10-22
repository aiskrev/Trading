"""Model client: the only place a model call is made. Sonnet 5.5 behind a narrow, tools-free transport.

Imports no execution, risk, orchestration or vault code (import-linter). The real SDK transport (network,
API key from a SecretsProvider) is a separate, separately approved step; this package never touches either.
"""
