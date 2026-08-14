"""Running the automation: the schedule, the kill switch, the live inbox read, and run history.

This was the `console/` package, which had its own FastAPI app and its own hand-rolled renderer on
port 8500. The app and the renderer are gone — everything Premier sees is now the one UI under
`/ui`, which is what these modules feed. What remains here is *logic only*, so it stays testable
without a browser and cannot grow a second interface by accident.

Nothing in this package writes to Premier's mailbox. That is not a convention: `tests/
test_operations_readonly.py` parses every module here and fails on an outbound write verb, on
`mark_processed`, and on any mailbox built without `read_only=True`. The Graph registration carries
`Mail.ReadWrite`, so this code is the only thing standing between the capability and Premier's
live Inbox.

State lives in `state/console.sqlite3` (run history and the schedule), deliberately apart from
`pipeline_state.sqlite3` — see `store.py`.
"""
