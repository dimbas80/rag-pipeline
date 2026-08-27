# Implementation report

## Summary
Fixed the provider-add request contract, settings status role source, split provider registry writing, and added regression coverage for index gating and chat formatting.

## Files changed
- `firmware/src/app.py`: accept scanned `{name, tag}` model entries, normalize them before provider registration, read combined providers for status, and write only role-relevant provider registries (while retaining unassigned providers in the create-markdown registry).
- `firmware/tests/test_app.py`: index gating and `settings_status` regression tests.
- `firmware/tests/test_chat_api.py`: `ChatSession._format` regression test with image selection mocked.

## Validation
- `python3 -m pytest firmware/tests/test_app.py firmware/tests/test_chat_api.py firmware/tests/test_providers_api.py -q`: 9 passed.
- `python3 -m pytest firmware/tests -q --ignore=firmware/tests/test_gap_filling.py`: 19 passed.
- `python3 -m compileall -q firmware/src firmware/tests`: passed.

## Known limitation
The pre-existing `firmware/tests/test_gap_filling.py` remains excluded as documented in the task context.
