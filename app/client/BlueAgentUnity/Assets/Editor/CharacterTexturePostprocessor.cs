using UnityEditor;
using UnityEngine;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// **立繪貼圖的匯入設定**（2026-10-16，owner 抓到的「角色被壓扁／橫向壓縮」的真因）。
    ///
    /// 真因不在畫面那層、也不在尺寸公式：**是 Unity 匯入時把圖壓扁了**。
    /// 我們的立繪 PNG 是**非 2 的次方**（`768x765`、`695x700`），而 Unity 預設的
    /// `Non-Power of 2 = ToNearest` 是**逐軸**縮放：768→1024、765→512
    /// ⇒ 進到遊戲裡變成 **1024x512（2:1）**，圖**本身**就被拉扁了。
    ///
    /// 真機 log 的憑據（`adb logcat -s Unity`，2026-10-16）：
    /// <code>
    ///   [stage] 立繪 mira texture=1024x512 box=342x487 drawn=285x143
    ///                                 ^^^^^^^^^^ 來源檔明明是 768x765
    /// </code>
    /// （畫面那層的 `drawn=285x143` 反而是**對的**：它忠實保持了那個被壓扁的 2:1 比例。）
    ///
    /// ⇒ 這裡把 `Resources/characters/**` 底下所有貼圖的 `npotScale` 關掉（**不縮放**），
    /// 並關掉 mipmap（立繪不縮小、不需要）。
    /// </summary>
    public sealed class CharacterTexturePostprocessor : AssetPostprocessor
    {
        /// <summary>立繪放這裡（`Assets/Resources/characters/&lt;id&gt;/animated-*.png`）。</summary>
        const string Folder = "/Resources/characters/";

        void OnPreprocessTexture()
        {
            var path = assetPath.Replace('\\', '/');
            if (path.IndexOf(Folder, System.StringComparison.Ordinal) < 0) return;

            var importer = (TextureImporter)assetImporter;
            // ⚠️ 這一條就是老闆看到的「壓扁」：預設會逐軸把 NPOT 縮成 2 的次方。
            importer.npotScale = TextureImporterNPOTScale.None;
            importer.textureType = TextureImporterType.Default;
            importer.mipmapEnabled = false;
            importer.wrapMode = TextureWrapMode.Clamp;
            importer.filterMode = FilterMode.Bilinear;
            importer.alphaIsTransparency = true;
            importer.maxTextureSize = 2048;
            Debug.Log($"[stage] 立繪貼圖匯入設定：{path}（NPOT 不縮放，避免被壓扁）");
        }
    }
}
