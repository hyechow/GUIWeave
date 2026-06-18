"""Prompt assets loaded from Markdown files with lightweight metadata."""

from .loader import PromptTemplate, iter_prompt_templates, load_prompt, load_prompt_text

__all__ = [
    "PromptTemplate",
    "iter_prompt_templates",
    "load_prompt",
    "load_prompt_text",
]
