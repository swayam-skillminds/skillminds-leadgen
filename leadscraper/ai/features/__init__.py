"""The AI features (Phase 5.4), each a module over :class:`leadscraper.ai.client.AIClient`.

Config time (``services/ai.py``, never auto-run): ``icp_builder``, ``planner``,
``source_discovery``. Run time (the local_business pipeline and sources):
``web_search`` (the ``ai_web_search`` source), ``normalisation``, ``summary``,
``signals`` (for the signal lead types of Phase 6). Extraction fallback and the
generic directory extractor live in ``ai.assistant`` and ``ai.selectors``.
"""
