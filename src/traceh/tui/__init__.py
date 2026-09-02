"""Optional Textual adapter for the UI-neutral Chat/Product mainline.

Package import intentionally loads neither Textual nor Rich. Core, Line Chat
and Evaluation installations therefore remain independent of the optional
``tui`` extra; callers import the concrete TUI modules they use.
"""
