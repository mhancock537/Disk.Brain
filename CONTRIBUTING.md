# Contributing

Thanks for looking. This started as a personal tool and it is shared because it
turned out to be useful. Issues and pull requests are welcome.

## Before you open a pull request

Run the tests:

```sh
.venv/bin/python -m pytest tests/
```

Tests that spawn a subprocess or load a model are marked `slow`. Skip them with
`-m "not slow"` while iterating, but run the full suite before you push.

## Ground rules

**Never commit `bundle/` or `data/`.** Both are gitignored. A concept file in
`bundle/` carries a document's title, an LLM-written summary of its contents, its
entities and its full source path. Committing one publishes a map of your disk.
If a change of yours makes those directories tracked again, that is a bug in the
change.

**Use fixtures, not your own documents.** `tests/conftest.py` builds a real
20-file corpus in a temporary directory for each run. Extend that if you need new
material. Do not paste in a real file, a real path or a real name.

**Keep it local.** No feature should send document content off the machine. That
constraint is the reason this exists, so a pull request that adds a cloud call
will not be merged, however convenient it is.

**Read-only against source files.** The indexer never writes, moves or deletes
anything it scans.

## Style

Match the surrounding code. The one thing worth knowing: `DECISIONS.md` records
the reasoning behind non-obvious choices. If you change something that file
explains, update the entry rather than leaving it stale. If you make a
non-obvious choice of your own, add one.

## Scope

Reasonable things to work on: more file formats, better extraction, a faster
reranker, packaging, Linux support.

Out of scope: anything that needs a hosted service, an account, or an API key to
work.
