"""Packaged runtime resources used by North.

The resource files are deliberately kept separate from Python implementation
modules.  ``utils.runtime_resources`` is the public resolver so callers do not
need to know whether North is running from a checkout or an installed wheel.
"""
