# Frozen stage-session fixtures

These are verbatim pre-foundation JSONL record shapes captured before the
canonical stage-session model landed. Their value is the durable schema
boundary: tests copy these bytes into Hopper's active or archived snapshot
path, load them through the real `load_lodes()` / `load_archived_lodes()`
normalizer, and only then inspect the result. Do not rebuild their dicts in a
test; that would bypass the production loader and make a compatibility break
look safe.

The legacy records discriminate fresh versus started state, direct versus
action archive fields, and the independence of the refine coder session from
the interactive-stage session. The agreeing hybrid freezes the two-projection
contract. The controls deliberately drop a required canonical field and alter
the canonical provider session against its legacy projection; both must fail
through the real loader. If either control starts loading, these fixtures no
longer guard the durable boundary.
