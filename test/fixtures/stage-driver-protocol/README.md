# Frozen stage-driver protocol fixtures

These JSON lines freeze the pre-foundation interactive-stage wire boundary.
They discriminate the markerless `connect` shape that can be inferred as
legacy Claude from malformed or incomplete handshakes, and the exact legacy
`lode_set_claude_started` mutation (which carries no provider, session, or
launch identity). Tests load these bytes from disk and drive the request or
mutation through a real Unix socket and Hopper's JSON serialization path; do
not rebuild the dicts in a test.

The connected response is intentionally bounded to the rolling-version
compatibility window. A dropped required field and any changed field type must
classify as unknown, never as legacy. The legacy start message must remain
accepted only when its current stage and run generation match the server's
durable lode.
