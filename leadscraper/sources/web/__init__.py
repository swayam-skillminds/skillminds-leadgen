"""What a website shows about itself (shared by lead types that look at sites).

``fingerprint.py`` + ``fingerprints.yaml``: which CMS, framework, shop or server a
site runs, which version, and whether that version is outdated. Used by
``digital_audit`` (its ``cms_age`` check) and ``tech_stack`` (Phase 8), which must
not depend on each other (A.3), so the rules live here.
"""
