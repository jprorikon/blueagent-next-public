using UnityEditor;
using UnityEngine;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// **3D 立繪的匯入設定**（`Assets/Resources/models3d/**`，owner 2026-10-30）。
    ///
    /// 為什麼要一支：這個專案**不進版控 `.meta`** ⇒ 每台建置機第一次匯入都是預設值，
    /// 匯入設定只能寫在程式裡（同 `CharacterTexturePostprocessor`）。
    ///
    /// 模型（外部模型網格，見 `blueagent/3d/README.txt`）：
    ///   * `Generic`：骨架是 Bip001（3ds Max Biped 命名），也沒有動畫要重定向；
    ///   * 不匯入 blendshape／相機／燈；材質照 FBX 匯入（執行期只拿它的**名字**對貼圖，
    ///     真正用的材質是 `NextStage3D` 現做的卡通著色 `BlueAgent/Stage3DToon`）。
    /// 貼圖：小圖（≤512）、要縮小顯示 ⇒ 開 mipmap；sRGB；不壓 NPOT。
    ///   alphaIsTransparency 開：嘴型圖集（`Character_Mouth`）是透明底，邊緣才不會滲出黑邊；不透明的貼圖不受影響。
    /// </summary>
    public sealed class Stage3DModelPostprocessor : AssetPostprocessor
    {
        const string Folder = "/Resources/models3d/";

        static bool Mine(string path) =>
            path.Replace('\\', '/').IndexOf(Folder, System.StringComparison.Ordinal) >= 0;

        void OnPreprocessModel()
        {
            if (!Mine(assetPath)) return;
            var importer = (ModelImporter)assetImporter;
            importer.globalScale = 1f;
            importer.useFileScale = true;
            importer.importBlendShapes = false;
            importer.importCameras = false;
            importer.importLights = false;
            importer.importVisibility = false;
            importer.importAnimation = false;
            importer.animationType = ModelImporterAnimationType.Generic;
            importer.avatarSetup = ModelImporterAvatarSetup.NoAvatar;
            importer.optimizeGameObjects = false;        // 執行期要照名字找骨頭
            importer.isReadable = true;                  // 執行期要把嘴搬到自己的子網格（`Stage3DRig.SplitMouth`）
            importer.importNormals = ModelImporterNormals.Import;
            importer.materialImportMode = ModelImporterMaterialImportMode.ImportStandard;
            importer.materialLocation = ModelImporterMaterialLocation.InPrefab;
            Debug.Log($"[stage3d] 模型匯入設定：{assetPath}");
        }

        void OnPreprocessTexture()
        {
            if (!Mine(assetPath)) return;
            var importer = (TextureImporter)assetImporter;
            importer.textureType = TextureImporterType.Default;
            importer.sRGBTexture = true;
            importer.npotScale = TextureImporterNPOTScale.None;
            importer.mipmapEnabled = true;
            importer.alphaIsTransparency = true;
            importer.filterMode = FilterMode.Bilinear;
            importer.maxTextureSize = 1024;
        }
    }
}
