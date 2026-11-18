using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 一顆 `Image` 自己擁有的執行期 Sprite（＋可選的 Texture2D）：物件銷毀或換圖時一起釋放
    /// （unity-review U-7，2026-10-28）。
    ///
    /// 以前相簿縮圖、聊天照片、全圖、棋盤格線、場景圖都直接 `Sprite.Create`，沒有任何 `Destroy` ⇒
    /// 反覆進出相簿／翻頁／看全圖，原生的 Texture／Sprite 只增不減。現在每一張都掛在顯示它的那顆物件上，
    /// `ClearBody()` 或關掉 overlay 時跟著走。從 `Resources` 載入的共用貼圖**不歸這裡管**（只放 Sprite）。
    /// 由 `NextUi.SetOwnedSprite` 建立，不直接用。
    /// </summary>
    public sealed class NextOwnedSprite : MonoBehaviour
    {
        private Sprite _sprite;
        private Texture2D _texture;

        public Sprite Sprite { get { return _sprite; } }

        /// <summary>換成新的一張；舊的那張（若是自己的）立刻釋放。</summary>
        public void Replace(Sprite sprite, Texture2D ownedTexture)
        {
            Release();
            _sprite = sprite;
            _texture = ownedTexture;
        }

        private void Release()
        {
            if (_sprite != null) Destroy(_sprite);
            if (_texture != null) Destroy(_texture);
            _sprite = null;
            _texture = null;
        }

        private void OnDestroy()
        {
            Release();
        }
    }
}
