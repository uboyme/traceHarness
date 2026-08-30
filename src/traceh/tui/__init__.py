"""Optional Textual adapter for the UI-neutral Chat/Product mainline.

This package intentionally imports no Textual module at package import time.
Core, Line Chat and Evaluation installations therefore remain independent of
the optional ``tui`` extra.
"""

from traceh.tui.presentation import product_observation_text, safe_display_block

__all__ = ["product_observation_text", "safe_display_block"]
