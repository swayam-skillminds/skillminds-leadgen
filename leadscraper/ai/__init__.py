"""Optional AI layer. Phase 5 splits this into providers/ and features/.

Everything public in ``assistant`` is re-exported, so ``from leadscraper.ai
import X`` keeps working from before the package existed.
"""

from .assistant import *  # noqa: F401,F403
from .assistant import AIAssistant, AIBackend, AIResult  # noqa: F401
