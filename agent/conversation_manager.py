"""
Manages multi-turn conversation state per session.
Persists messages + requirements in Supabase sessions table.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.supabase_client import get_session, save_session

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20  # keep last 20 messages per session


class ConversationManager:
    def __init__(self, session_id: str, platform: str = "web"):
        self.session_id = session_id
        self.platform = platform
        self._loaded = False
        self.messages: list[dict] = []
        self.requirements: dict = {}
        self.stage: str = "discovery"

    def load(self) -> None:
        """Load session from Supabase."""
        session = get_session(self.session_id)
        self.messages = session.get("messages") or []
        raw_req = session.get("requirements") or {}
        self._recommendation_count: int = int(raw_req.pop("_recommendation_count", 0))
        self.requirements = raw_req
        self.stage = session.get("stage") or "discovery"
        self._loaded = True
        logger.debug(f"Loaded session {self.session_id}: stage={self.stage}, messages={len(self.messages)}")

    def save(self) -> None:
        """Persist current state back to Supabase."""
        save_session(
            session_id=self.session_id,
            messages=self.messages[-MAX_HISTORY_MESSAGES:],
            requirements={**self.requirements, "_recommendation_count": self._recommendation_count},
            stage=self.stage,
        )

    def get_recommendation_count(self) -> int:
        return getattr(self, "_recommendation_count", 0)

    def increment_recommendation_count(self) -> None:
        self._recommendation_count = getattr(self, "_recommendation_count", 0) + 1

    def add_user_message(self, content: str) -> None:
        self.messages.append({
            "role": "user",
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({
            "role": "assistant",
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def get_history_for_llm(self) -> list[dict]:
        """Return messages in the format expected by Groq/OpenAI chat API."""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.messages[-MAX_HISTORY_MESSAGES:]
        ]

    def update_requirements(self, new_requirements: dict) -> None:
        from agent.intent_extractor import merge_requirements
        self.requirements = merge_requirements(self.requirements, new_requirements)

    def set_stage(self, stage: str) -> None:
        valid_stages = {"discovery", "recommending", "lead_capture", "done"}
        if stage in valid_stages:
            self.stage = stage
        else:
            logger.warning(f"Unknown stage: {stage}")

    def has_enough_info(self) -> bool:
        """Check if we have minimum info to run a property search."""
        req = self.requirements
        return bool(req.get("bhk") or req.get("area") or req.get("max_budget_cr"))

    def is_lead_capture_stage(self) -> bool:
        return self.stage == "lead_capture"
