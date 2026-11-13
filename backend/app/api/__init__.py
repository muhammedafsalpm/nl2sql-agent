"""FastAPI layer for the natural-language-to-SQL assistant.

The agent graph, prompts, DB access and safety guards are reused UNCHANGED from `app/`.
This package only exposes them over HTTP; the terminal CLI (`python main.py`) keeps working
exactly as before.
"""
