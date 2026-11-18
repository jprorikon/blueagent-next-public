"""兼容转发：贴纸目录与素材的 canonical 归属在 ``app.client.assets``。"""

from app.client.assets.stickers import MIME, Sticker, StickerCatalog

__all__ = ["MIME", "Sticker", "StickerCatalog"]
