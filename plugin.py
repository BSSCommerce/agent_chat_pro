"""Agent Chat Pro plugin registration."""

from __future__ import annotations

from fastapi import APIRouter

from agent_chat_pro.models import (
    AgentChatProMessage,
    AgentChatProThread,
    AgentChatProUserPreference,
)
from agent_chat_pro.router import router
from core.plugin_sdk.base import MenuItem, PluginBase, PluginMeta


class AgentChatProPlugin(PluginBase):
    """Professional single-agent chat with persisted thread history."""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="agent_chat_pro",
            version="0.1.0",
            description="Professional single-agent chat UI with per-user threads and preferences.",
            author="community",
            dependencies=["agents_admin"],
        )

    def models(self):
        return [
            AgentChatProThread,
            AgentChatProMessage,
            AgentChatProUserPreference,
        ]

    def routers(self) -> list[APIRouter]:
        return [router]

    def menu_items(self) -> list[MenuItem]:
        return [
            MenuItem(
                label="Agent Chat Pro",
                url="/agent-chat-pro",
                icon="message-square",
                order=61,
                key="community_agent_chat_pro",
                parent_key="community",
            )
        ]
