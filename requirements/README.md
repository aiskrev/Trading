# Hash-locked dependencies

Every package that can run is pinned to an exact version **and** its SHA-256 hashes (threat T14, supply chain).
`pip` refuses anything that does not match (`--require-hashes`).

| file | contents | install it for |
|---|---|---|
| `runtime.lock` | the trading process (pydantic, pyyaml, cryptography, tzdata) | running the pipeline, the vault CLI |
| `model.lock` | runtime + the `anthropic` SDK | running the model client and `scripts/run_model_eval.py` |
| `dev.lock` | runtime + pytest, hypothesis, import-linter | tests, CI, the pre-push gate |
| `sbom.cdx.json` | CycloneDX 1.5 bill of materials generated from `model.lock` | review and audit |

Python **3.12** is the floor and the CI version. The same locks are verified to install and pass the whole suite on 3.12.15 and 3.14.4 (Ubuntu 26.04 ships 3.14 and no 3.12 package).

## Install

```bash
python3.12 -m venv ~/venvs/trading
~/venvs/trading/bin/pip install --require-hashes -r requirements/dev.lock     # tests and development
~/venvs/trading/bin/pip install --require-hashes -r requirements/model.lock   # a machine that calls the model
~/venvs/trading/bin/pip install --no-deps -e .
```

## Update (a deliberate act, reviewed like code)

Regenerate on Linux with Python 3.12, review the diff of the lock files, run the whole suite, then commit:

```bash
pip install pip-tools
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements/runtime.lock pyproject.toml
pip-compile --generate-hashes --strip-extras --allow-unsafe --extra model -o requirements/model.lock pyproject.toml
pip-compile --generate-hashes --strip-extras --allow-unsafe --extra dev -o requirements/dev.lock pyproject.toml
python scripts/make_sbom.py
```

`tests/security/test_dependency_lock.py` fails if a lock file has an unpinned, unhashed, URL or editable entry, if a
declared dependency is missing from the lock, or if the SBOM no longer matches `model.lock`.

## Not done yet

* GitHub Actions in `.github/workflows/ci.yml` are pinned to major tags, not commit SHAs.
* `pip` itself and the build backend (`setuptools`) are not in the locks; CI upgrades `pip` before installing.
* A vulnerability scan of the locked versions (`pip-audit`) is a separate step to add.
