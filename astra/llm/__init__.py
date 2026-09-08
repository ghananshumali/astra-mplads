"""Optional LLM provider layer.

Isolated behind a small interface so the provider or model can be swapped
without touching the synthesis logic or anything upstream of it.
"""
from .provider import LLMResult, available, complete_json, provider_status

__all__ = ["LLMResult", "available", "complete_json", "provider_status"]
