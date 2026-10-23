"""Control room: a local, read-mostly web UI for the trading agent project.

Rules (enforced by tests/ui/test_ui_security.py):
  * it listens on 127.0.0.1 only and checks the Host and Origin headers;
  * it cannot change the mode, any config, an approval, the kill switch, a prompt or a secret, and it has no route that
    could submit an order or call the model;
  * it imports no execution, risk-gateway, signing, vault or model-client code;
  * it can start only a short fixed list of local commands (the standing checks, a dataset backtest, a demo run).
"""
