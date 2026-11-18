"""不依赖 Agent 的公共区社交核心。"""

from app.server.social.models import Actor, BoardError, Post
from app.server.social.service import BoardService

__all__ = ["Actor", "BoardError", "BoardService", "Post"]
