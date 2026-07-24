"""Stable public facade for durable operation lifecycle coordination."""

from .manager_merge_impl import OperationManager

__all__ = ["OperationManager"]
