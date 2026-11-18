// ⚠️ **不要**包 `#if UNITY_IOS`（2026-10-19 實測踩到）：編輯器腳本是按「當下的 ActiveBuildTarget」
//    編譯的；批處理建置 iOS 時 active target 還是 Android ⇒ 整個檔案被編譯掉、PostProcessBuild 從不執行，
//    症狀是「Unity 說 Build result=Succeeded，但打出來的 Info.plist 一個補丁鍵都沒有」。
//    ⇒ 無條件編進來，靠 callback 裡的 `target != BuildTarget.iOS` 早退。
using System.IO;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEditor.iOS.Xcode;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// iOS 的 Info.plist 補丁（Unity 的 PlayerSettings 沒有這幾個鍵）。
    ///
    /// 為什麼一定要有（2026-10-19 真機）：
    /// <list type="bullet">
    /// <item><b>NSLocalNetworkUsageDescription</b>：iOS 14 起，連**區網**（橋、心跳、Discord 都在區網）
    ///   要使用者授權。**沒有這個字串，連提示都不會出現，連線直接被靜默拒絕** ——
    ///   症狀是「app 起來了、畫面正常、但橋永遠連不上、心跳一包也收不到」。
    ///   有了它，第一次連線時系統會彈「是否允許 XXX 尋找並連接本地網路上的裝置」。</item>
    /// <item><b>UIBackgroundModes=audio</b>：鎖屏/切到別 app 時繼續收音與播放（她是「一直聽」的角色）。</item>
    /// </list>
    ///
    /// ⚠️ 這支只在**建置時**跑（`<c>-batchmode -executeMethod</c>` 那條路也會跑 PostProcessBuild）。
    /// 改完的 plist 在 Xcode 專案裡，下一次 Unity 重建會重新生成 ⇒ 補丁必須留在這裡，別手改 plist。
    /// </summary>
    public static class IosPlistPostProcess
    {
        [PostProcessBuild(1000)]
        public static void OnPostProcessBuild(BuildTarget target, string pathToBuiltProject)
        {
            if (target != BuildTarget.iOS) return;

            var plistPath = Path.Combine(pathToBuiltProject, "Info.plist");
            if (!File.Exists(plistPath))
            {
                UnityEngine.Debug.LogWarning($"[ios] 找不到 {plistPath} ⇒ 沒補 plist 鍵");
                return;
            }

            var plist = new PlistDocument();
            plist.ReadFromFile(plistPath);
            var root = plist.root;

            root.SetString("NSLocalNetworkUsageDescription",
                "BlueAgent 要連你自己設定的對話橋（同一個區網），不然她聽不到也回不了。");
            root.SetString("NSLocationWhenInUseUsageDescription",
                "BlueAgent 只用大約位置更新天氣與世界變化，不需要精確位置。");
            root.SetBoolean("NSLocationDefaultAccuracyReduced", true);

            var modes = root["UIBackgroundModes"]?.AsArray() ?? root.CreateArray("UIBackgroundModes");
            var hasAudio = false;
            foreach (var v in modes.values)
                if (v != null && v.AsString() == "audio") hasAudio = true;
            if (!hasAudio) modes.AddString("audio");

            // 區網發現（Bonjour）目前不需要；若以後要自動找橋再加 NSBonjourServices。

            plist.WriteToFile(plistPath);
            UnityEngine.Debug.Log($"[ios] Info.plist 補丁完成：NSLocalNetworkUsageDescription ＋ UIBackgroundModes=audio（{plistPath}）");
            LinkFrameworks(pathToBuiltProject);
        }

        /// <summary>
        /// 外掛要的系統框架（Unity 預設不連）：`PhotosUI`＝選圖的 `PHPickerViewController`
        /// （`Plugins/iOS/BlueAgentPicker.mm`）；`Security`＝登入 token 存鑰匙圈（`Plugins/iOS/BlueAgentKeychain.mm`）。沒連上 ⇒ 連結期 `_OBJC_CLASS_$_PHPickerViewController` 找不到
        /// （2026-10-29 模擬器出包實測；`#import` 的自動連結對 Unity 外掛那幾支不生效）。
        /// </summary>
        private static void LinkFrameworks(string pathToBuiltProject)
        {
            var projectPath = PBXProject.GetPBXProjectPath(pathToBuiltProject);
            var project = new PBXProject();
            project.ReadFromFile(projectPath);
            var framework = project.GetUnityFrameworkTargetGuid();
            project.AddFrameworkToProject(framework, "PhotosUI.framework", false);
            project.AddFrameworkToProject(framework, "Security.framework", false);
            project.WriteToFile(projectPath);
            UnityEngine.Debug.Log("[ios] 連結框架：PhotosUI、Security");
        }
    }
}
