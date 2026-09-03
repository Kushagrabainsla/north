"""Fetched model facts - the data north routes on.

Model choice used to be inferred from the model *id* (name tokens, a substring
tier table). This package replaces that with data downloaded from catalog
sources, joined on a canonical model identity and merged by how trustworthy each
source is. Nothing here knows what a "good" model is called; it only knows what
the sources said, when, and how confidently.

  identity.py  the join key across sources and providers
  models.py    Fact/Rank/ModelFacts/Endpoint - the vocabulary
  merge.py     rank-based merge, provenance, the unknown-score prior
  sources/     one parser per catalog source
  store.py     ~/.north/models.db - the durable, offline-capable cache
  catalog.py   the refresh pipeline and the in-memory snapshot routing reads
"""
