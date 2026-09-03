"""One parser per catalog source. Each turns a raw payload into facts and endpoints.

A source module only ever *reads*: it does no merging, no ranking and no
persistence, so adding a source is adding a file rather than editing the
pipeline.
"""
