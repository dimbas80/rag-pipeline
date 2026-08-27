# Implementation report — t_96b0be8e

## Summary
Closed the final review findings. Provider role settings are now exposed as a merged view but persisted to the provider file belonging to each pipeline. Added the provider modal/scan/add flow, server-side convert gating, image captions, and regression tests.

## Files changed
- `firmware/src/app.py`: merged provider reads, split writes, convert role/API-key gating.
- `firmware/src/chat_api.py`: preserve captions in image response contract.
- `firmware/src/static/index.html`: provider dialog.
- `firmware/src/static/app.js`: provider scan/add/cancel handlers.
- `firmware/tests/test_app.py`, `firmware/tests/test_chat_api.py`: traversal/masking/caption coverage.

## Validation
- `python3 -m pytest firmware/tests -q` — 16 passed.
- `python3 -c 'from firmware.src import app; print(len(app.app.routes))'` — 33 routes.
- `git diff --check` — passed.

## Limitations
Provider add stores the submitted API key through the existing masked `.env` settings route; the providers registry stores only `api_key_env`. Real provider scanning requires a reachable OpenAI-compatible `/models` endpoint.
