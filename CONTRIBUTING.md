# Contributing

Thanks for helping. A few ground rules keep harvester trustworthy:

1. **Politeness is not optional.** Changes must not add ways to evade blocks:
   no fingerprint spoofing, CAPTCHA solving or block-dodging proxy rotation.
2. **Callbacks stay pure.** Anything that would let a parse callback do I/O breaks offline re-parsing.
3. **Tests come with changes.** `pytest`, `ruff check .`, `ruff format --check .` and `mypy src` must pass.

New sources for public bulk-data protocols (CKAN, Zenodo, sitemaps …) are especially welcome.
