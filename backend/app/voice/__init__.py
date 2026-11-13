"""Optional voice layer (Speech-to-Text + Text-to-Speech).

Entirely additive: the chat pipeline never imports this package, and removing the folder plus the
three-line registration in app/api/main.py removes the feature completely.
"""
from app.voice.routes import router

__all__ = ["router"]
