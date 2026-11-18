using UnityEditor;
using UnityEngine;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// **介面圖示的匯入設定**（`Resources/icons/`：預烤的 SVG → PNG，96 見方、白色＋alpha，執行期用 tint 上色）。
    ///
    /// 為什麼要它（2026-10-19，owner：「曲線元素的鋸齒感很強」）：先前走預設匯入 ⇒ Android 上被壓成
    /// **ASTC**（小圖示的 alpha 邊緣變成一塊一塊）、`alphaIsTransparency` 關著（雙線性取樣時透明像素的
    /// 黑色會滲進邊緣）。圖示只有二十來張、每張 36 KB ⇒ 不壓縮、開 mipmap（96 縮成 30 多 px 不閃）、三線性。
    /// </summary>
    public sealed class UiIconTexturePostprocessor : AssetPostprocessor
    {
        const string Folder = "/Resources/icons/";

        // 改了規則就加一 ⇒ Unity 會把這一族貼圖重新匯入。
        public override uint GetVersion() => 1;

        void OnPreprocessTexture()
        {
            var path = assetPath.Replace('\\', '/');
            if (path.IndexOf(Folder, System.StringComparison.Ordinal) < 0) return;
            var importer = (TextureImporter)assetImporter;
            importer.textureType = TextureImporterType.Default;
            importer.textureCompression = TextureImporterCompression.Uncompressed;
            importer.alphaIsTransparency = true;
            importer.mipmapEnabled = true;
            importer.filterMode = FilterMode.Trilinear;
            importer.wrapMode = TextureWrapMode.Clamp;
            importer.npotScale = TextureImporterNPOTScale.None;
        }
    }
}
