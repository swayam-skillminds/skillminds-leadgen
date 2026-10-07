"""AI providers behind one interface (Phase 5.1): see ``base`` and ``registry``."""

from .base import (  # noqa: F401
    AIRequest, AIResponse, AuthFailed, Capabilities, NotConfigured, Provider, ProviderError,
    ProviderSpec, QuotaExhausted, Refused,
)
from .registry import KINDS, build, make_spec  # noqa: F401
