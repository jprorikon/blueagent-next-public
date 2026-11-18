using System;
using System.IO;
using BlueAgent.Next;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEditor.XR.Management;
using UnityEditor.XR.Management.Metadata;
using UnityEditor.XR.OpenXR.Features;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;
using UnityEngine.XR.Management;
using UnityEngine.XR.OpenXR;
using UnityEngine.XR.OpenXR.Features.CompositionLayers;
using UnityEngine.XR.OpenXR.Features.Interactions;
using UnityEngine.XR.Hands.OpenXR;
using UnityEngine.XR.OpenXR.Features.Meta;
using UnityEngine.XR.OpenXR.Features.MetaQuestSupport;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// Quest 3 混合現實端的場景與出包。Quest 專有程式與設定在頂層 /mr；
    /// 建置前由 mr/tools/sync_workspace.py 組合 app/client 的共享執行期，倉庫不複製第二份。
    ///
    /// MR 建置會掛 OpenXR loader、開 Meta 的 AR 功能，並拿掉共享 Assets 帶來的手機 androidlib
    /// （主屏 launcher、無障礙服務、前景服務都不該進頭顯），建完在 finally 裡還原工作區設定。
    /// </summary>
    public static class MrBuild
    {
        private const string ScenePath = "Assets/Scenes/MR.unity";
        /// <summary>與手機包（`.next`）不同 ⇒ 兩個 app 能並存；只侧載、不上 Store（PLAN §3 M5）。</summary>
        private const string BundleId = "dev.deepsea.blueagent.mr";
        private const string PhonePlugin = "Assets/Plugins/Android/blueagent-permissions.androidlib";
        /// <summary>MR 專用：手部追蹤權限＋uses-feature、麥克風（M3）。跟手機那個正好相反：MR 掛、手機拿掉。</summary>
        private const string MrPlugin = "Assets/Plugins/Android/blueagent-mr.androidlib";
        private const string XrAssetDir = "Assets/XR";

        /// <summary>
        /// 要開的 OpenXR 功能：Quest 支援（manifest、裝置清單）、Meta 的 Session／Camera（passthrough）／Planes／Ray casts、
        /// 刷新率（儀表要讀）、手柄 profile（不開的話 OpenXR 驗證會報沒有任何 interaction profile）；
        /// M1 加 Anchors（持久錨點）與 Occlusion（環境深度）。Meshing／Bounding boxes 用不到、不開。
        ///
        /// ⚠️ **Composition Layers 一定要開**（2026-10-31 實機踩到）：Unity OpenXR Meta 2.x 的 passthrough 是一層
        /// OpenXR 合成層，相機子系統要等 `OpenXRLayerProvider.Started` 才註冊那一層——而 provider 只有這個功能會建。
        /// 沒開 ⇒ 遮擋、平面、錨點全都正常，**唯獨背景一片黑**，建置驗證與 log 都不報。
        /// </summary>
        private static readonly Type[] Features =
        {
            typeof(MetaQuestFeature),
            typeof(OpenXRCompositionLayersFeature),
            typeof(ARSessionFeature),
            typeof(ARCameraFeature),
            typeof(ARPlaneFeature),
            typeof(ARRaycastFeature),
            typeof(DisplayUtilitiesFeature),
            typeof(ARAnchorFeature),
            typeof(AROcclusionFeature),
            typeof(HandTracking),                                 // M3：捏手指說話、捏住她搬走（XR Hands）
            typeof(OculusTouchControllerProfile),
            typeof(MetaQuestTouchPlusControllerProfile),
        };

        /// <summary>MR 的場景：只掛 `MrShell`（XR Origin、AR 管理器、相機都由它執行期組裝）。</summary>
        [MenuItem("BlueAgent/MR シーンを作り直す")]
        public static void CreateMrScene()
        {
            Directory.CreateDirectory("Assets/Scenes");
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            new GameObject("MrShell", typeof(BlueAgent.MR.MrShell));
            EditorSceneManager.SaveScene(scene, ScenePath);
            Debug.Log($"[mr] 場景好了：{ScenePath}（只掛 MrShell）");
        }

        /// <summary>
        /// 建 Quest 3 的 MR 包（Android／arm64／IL2CPP；OpenXR）。輸出路徑 `BA_MR_APK_OUT`（預設 `Builds/BlueAgentMR.apk`）。
        /// </summary>
        public static void BuildMrQuest()
        {
            // OpenXR 的建置驗證（MetaQuestFeature 的規則）讀的是「目前選中的」target group：
            // 無頭跑時若停在 Standalone ⇒ 那裡沒有 Quest 功能 ⇒ NullReferenceException，其餘驗證結果全被吞掉
            // （2026-10-31 第一次出包實測：Build 照樣 Succeeded，但 errors=1）。
            if (EditorUserBuildSettings.activeBuildTarget != BuildTarget.Android)
                EditorUserBuildSettings.SwitchActiveBuildTarget(BuildTargetGroup.Android, BuildTarget.Android);
            HeadlessBuild.EnsureRenderPipeline();
            DisableHdr();
            HeadlessBuild.EnsureHttpAllowed();
            // 執行期 Shader.Find 的兩支：接地陰影、軟遮擋的深度預處理（AR Foundation 附的）
            HeadlessBuild.EnsureAlwaysIncludedShaders(BlueAgent.MR.MrGroundShadow.ShaderName,
                                                      BlueAgent.MR.MrShell.SoftOcclusionShader);
            CreateMrScene();

            BuildReport report = null;
            try
            {
                if (!UseXr(true) || !EnableFeatures())
                {
                    EditorApplication.Exit(2);
                    return;
                }
                UsePlugin(PhonePlugin, false);
                UsePlugin(MrPlugin, true);
                ConfigurePlayer();

                var outPath = Environment.GetEnvironmentVariable("BA_MR_APK_OUT");
                if (string.IsNullOrEmpty(outPath)) outPath = Path.GetFullPath("Builds/BlueAgentMR.apk");
                Directory.CreateDirectory(Path.GetDirectoryName(outPath));

                report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
                {
                    scenes = new[] { ScenePath },
                    locationPathName = outPath,
                    target = BuildTarget.Android,
                    options = BuildOptions.None,
                });
                Debug.Log($"[mr] Build result={report.summary.result} size={report.summary.totalSize} " +
                          $"errors={report.summary.totalErrors} ⇒ {outPath}");
            }
            finally
            {
                UsePhoneSetup();
            }
            if (report == null || report.summary.result != BuildResult.Succeeded) EditorApplication.Exit(2);
        }

        /// <summary>手機包的前置（`HeadlessBuild.BuildNext` 的 Android 那一支呼叫）：摘掉 XR loader、androidlib 掛回去。</summary>
        internal static void UsePhoneSetup()
        {
            UseXr(false);
            UsePlugin(PhonePlugin, true);
            UsePlugin(MrPlugin, false);
        }

        /// <summary>
        /// passthrough 要相機畫面的 alpha：HDR 的色彩緩衝（R11G11B10）**沒有 alpha** ⇒ 現實透不出來、一片黑。
        /// 管線資產每次建置都由 `EnsureRenderPipeline` 重建，所以這裡只改 MR 這一次，不影響手機包。
        /// </summary>
        private static void DisableHdr()
        {
            if (!(GraphicsSettings.defaultRenderPipeline is UniversalRenderPipelineAsset pipeline)) return;
            pipeline.supportsHDR = false;
            EditorUtility.SetDirty(pipeline);
            AssetDatabase.SaveAssets();
            Debug.Log("[mr] URP：關 HDR（passthrough 要 alpha）");
        }

        private static void ConfigurePlayer()
        {
            var named = NamedBuildTarget.Android;
            PlayerSettings.companyName = "BlueAgent";
            PlayerSettings.productName = "BlueAgent MR";
            PlayerSettings.SetApplicationIdentifier(named, BundleId);
            PlayerSettings.Android.forceInternetPermission = true;
            PlayerSettings.SetScriptingBackend(named, ScriptingImplementation.IL2CPP);
            PlayerSettings.Android.targetArchitectures = AndroidArchitecture.ARM64;
            // Meta 的 OpenXR runtime 要 Android 12L（API 32）以上；手機包在 BuildNext 裡自己設回 26
            PlayerSettings.Android.minSdkVersion = AndroidSdkVersions.AndroidApiLevel32;
            PlayerSettings.SetManagedStrippingLevel(named, ManagedStrippingLevel.Minimal);
            EditorUserBuildSettings.buildAppBundle = false;
            EditorUserBuildSettings.development = false;
            EditorUserBuildSettings.allowDebugging = false;
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.LandscapeLeft;
            HeadlessBuild.ConfigureAndroidSigning();
            Debug.Log($"[mr] PlayerSettings：{BundleId}／IL2CPP arm64／minSdk 32");
        }

        /// <summary>Android 掛上（或摘掉）OpenXR loader。設定資產不在就建（摘的時候不在就不必建）。</summary>
        private static bool UseXr(bool on)
        {
            var perTarget = PerTargetSettings(create: on);
            if (perTarget == null) return true;
            var group = BuildTargetGroup.Android;
            if (!perTarget.HasSettingsForBuildTarget(group)) perTarget.CreateDefaultSettingsForBuildTarget(group);
            if (!perTarget.HasManagerSettingsForBuildTarget(group)) perTarget.CreateDefaultManagerSettingsForBuildTarget(group);
            var settings = perTarget.SettingsForBuildTarget(group);
            var manager = perTarget.ManagerSettingsForBuildTarget(group);
            settings.InitManagerOnStart = on;

            var loader = typeof(OpenXRLoader).FullName;
            var has = manager.activeLoaders.Count > 0;
            var ok = on ? XRPackageMetadataStore.AssignLoader(manager, loader, group)
                        : !has || XRPackageMetadataStore.RemoveLoader(manager, loader, group);
            EditorUtility.SetDirty(settings);
            EditorUtility.SetDirty(manager);
            EditorUtility.SetDirty(perTarget);
            AssetDatabase.SaveAssets();
            if (!ok)
            {
                Debug.LogError($"[mr] OpenXR loader {(on ? "掛上" : "摘掉")}失敗（Android）");
                return false;
            }
            Debug.Log($"[mr] Android XR loader：{(on ? "OpenXR" : "無")}（現在 {manager.activeLoaders.Count} 個）");
            return true;
        }

        /// <summary>
        /// 找 XR Management 的設定資產（EditorBuildSettings 裡登記的 → 專案裡任一份 → 新建）。
        /// 建置機的鏡像同步會清掉倉庫沒有的 `Assets/XR`，所以每次都可能是從零開始，這支要能重入。
        /// </summary>
        private static XRGeneralSettingsPerBuildTarget PerTargetSettings(bool create)
        {
            if (EditorBuildSettings.TryGetConfigObject(XRGeneralSettings.settingsKey,
                                                      out XRGeneralSettingsPerBuildTarget found) && found != null)
                return found;
            var guids = AssetDatabase.FindAssets("t:XRGeneralSettingsPerBuildTarget");
            if (guids.Length > 0)
            {
                found = AssetDatabase.LoadAssetAtPath<XRGeneralSettingsPerBuildTarget>(AssetDatabase.GUIDToAssetPath(guids[0]));
                if (found != null)
                {
                    EditorBuildSettings.AddConfigObject(XRGeneralSettings.settingsKey, found, true);
                    return found;
                }
            }
            if (!create) return null;
            Directory.CreateDirectory(XrAssetDir);
            var created = ScriptableObject.CreateInstance<XRGeneralSettingsPerBuildTarget>();
            AssetDatabase.CreateAsset(created, XrAssetDir + "/XRGeneralSettingsPerBuildTarget.asset");
            EditorBuildSettings.AddConfigObject(XRGeneralSettings.settingsKey, created, true);
            AssetDatabase.SaveAssets();
            return created;
        }

        /// <summary>只開 <see cref="Features"/> 那幾個；其餘照套件預設（M0 不碰）。立體一律單趟（multiview）。</summary>
        private static bool EnableFeatures()
        {
            var group = BuildTargetGroup.Android;
            FeatureHelpers.RefreshFeatures(group);
            var settings = OpenXRSettings.GetSettingsForBuildTargetGroup(group);
            if (settings == null)
            {
                Debug.LogError("[mr] 讀不到 Android 的 OpenXR 設定");
                return false;
            }
            foreach (var type in Features)
            {
                var feature = settings.GetFeature(type);
                if (feature == null)
                {
                    Debug.LogError($"[mr] 找不到 OpenXR 功能 {type.Name}（套件版本不對？）");
                    return false;
                }
                feature.enabled = true;
                EditorUtility.SetDirty(feature);
            }
            settings.renderMode = OpenXRSettings.RenderMode.SinglePassInstanced;
            EditorUtility.SetDirty(settings);
            AssetDatabase.SaveAssets();
            Debug.Log($"[mr] OpenXR 功能：{string.Join("、", Array.ConvertAll(Features, t => t.Name))}（單趟立體）");
            return true;
        }

        /// <summary>某個 androidlib 進不進 Android 包（手機那個：MR 不要、手機要；MR 那個反過來）。</summary>
        private static void UsePlugin(string path, bool on)
        {
            if (!(AssetImporter.GetAtPath(path) is PluginImporter importer))
            {
                Debug.LogWarning($"[mr] ⚠️ {path} 不是 plugin（或不存在）⇒ 沒法{(on ? "掛上" : "拿掉")}");
                return;
            }
            if (importer.GetCompatibleWithPlatform(BuildTarget.Android) == on) return;
            importer.SetCompatibleWithPlatform(BuildTarget.Android, on);
            importer.SaveAndReimport();
            Debug.Log($"[mr] {path}：Android {(on ? "掛上" : "拿掉")}");
        }

        /// <summary>
        /// 無頭自檢（不用頭顯）：照 MR 的身高把角色生在原點，驗 `Stage3DRig.Spawn(height)` 縮出來的身高對不對
        /// （差 &lt; 5 mm；量 bounds 時縮放只能算一次），順便印腳底在原點上下多少（MR 召喚後要補的那一段）。用法同 `Stage3DPreview`（不要配 `-quit`）。
        /// </summary>
        public static void CheckRig()
        {
            var failed = false;
            foreach (var character in new[] { BlueAgent.MR.MrShell.Character, "nori", "aster" })
            {
                if (!NextStage3D.Has(character)) continue;
                var native = Stage3DRig.Spawn(character, Vector3.zero, 0);
                var nativeHeight = native.Bounds.size.y;
                native.Dispose();

                var rig = Stage3DRig.Spawn(character, Vector3.zero, 0, BlueAgent.MR.MrShell.Height);
                var height = rig.Bounds.size.y;
                var sole = rig.SoleY;
                var ok = Mathf.Abs(height - BlueAgent.MR.MrShell.Height) < 0.005f;
                failed |= !ok;
                Debug.Log($"[mr] check {character}: native={nativeHeight:F3} m scaled={height:F3} m "
                          + $"(want {BlueAgent.MR.MrShell.Height:F3}) boundsMin={rig.Bounds.min.y:F4} sole={sole:F4} "
                          + $"scale={rig.Root.transform.localScale.x:F3} ⇒ {(ok ? "OK" : "NG")}");
                rig.Dispose();
            }
            EditorApplication.Exit(failed ? 1 : 0);
        }

        /// <summary>
        /// 無頭自檢 M2 ②「待機 60 秒不抖」：真的 MAGIC 待機（跟頭顯上同一支）＋看著正前上方的你，72 fps 跑滿 60 秒，
        /// 量頭（相對身體）的角速度。**判據：沒有一幀超過 180°/s**（72 fps 下一幀 2.5°）＝沒有「啪一下」的甩頭；
        /// 超過 90°/s 的幀數照樣印出來（MAGIC 待機會「看旁邊」，快速但連續的轉頭不算抖）。
        /// 2026-11-01：VAR 待機沒限速時最快 538°/s（一幀跳 7°），限速 45°/s 後最快約 157°/s、連續幾幀而不是跳一下。
        /// 穿模（袖子穿進身體之類）量不了，要眼睛看。用法同 <see cref="CheckRig"/>（不要配 `-quit`）。
        /// </summary>
        public static void CheckIdle()
        {
            var idleWas = NextConfig.IdleMotion;
            var failed = false;
            try
            {
                // 三種對照：真的待機＋看人（頭顯上就是這個）、只有待機、正弦待機＋看人 ⇒ 抖從哪一層來一眼看得出
                failed = IdleRun("MAGIC+look", true, true);
                IdleRun("MAGIC only", true, false);
                IdleRun("sine+look", false, true);
            }
            finally
            {
                NextConfig.IdleMotion = idleWas;
            }
            EditorApplication.Exit(failed ? 1 : 0);
        }

        private static bool IdleRun(string label, bool magic, bool look)
        {
            NextConfig.IdleMotion = magic;
            var rig = Stage3DRig.Spawn(BlueAgent.MR.MrShell.Character, Vector3.zero, 0, BlueAgent.MR.MrShell.Height);
            var front = Vector3.ProjectOnPlane(rig.Forward, Vector3.up).normalized;
            if (look) rig.LookTarget = rig.HeadBone.position + front * 0.6f + Vector3.up * 0.45f;   // 坐在桌前的你
            const float dt = 1f / 72f;
            const float settle = 1f;
            var fastest = 0f;
            var fast = new System.Collections.Generic.List<string>();
            var last = Quaternion.identity;
            var frames = 0;
            for (var time = 0f; time < 60f + settle; time += dt)
            {
                rig.Animate(time, dt, 0f, 0f, 0f);
                var now = Quaternion.Inverse(rig.Root.transform.rotation) * rig.HeadBone.rotation;
                if (time > settle)
                {
                    var speed = Quaternion.Angle(last, now) / dt;
                    fastest = Mathf.Max(fastest, speed);
                    if (speed > 90f) fast.Add($"{time:F2}s={speed:F0}");
                    frames++;
                }
                last = now;
            }
            var bad = fastest > 180f;
            Debug.Log($"[mr] idle 60s @72fps [{label}]：{frames} 幀、頭最快 {fastest:F1}°/s、超過 90°/s 的幀 {fast.Count}"
                      + (fast.Count > 0 ? "（" + string.Join(", ", fast) + "）" : "") + $" ⇒ {(bad ? "NG" : "OK")}");
            rig.Dispose();
            return bad;
        }

        /// <summary>
        /// 無頭自檢 M2「她看你」：同一個角色生兩份（同樣的確定性待機；一份有 LookTarget、一份沒有），跑 3 秒，
        /// 兩份頭骨的世界旋轉差＝看人那一層加上去的轉動 ⇒ 把「身體正前方」轉過去，量它實際指向的左右／上下角，
        /// 跟目標（超過上限就是上限）比，差 &lt; 3° 才算過。用法同 <see cref="CheckRig"/>（不要配 `-quit`）。
        /// </summary>
        public static void CheckLook()
        {
            var idleWas = NextConfig.IdleMotion;
            NextConfig.IdleMotion = false;                       // 正弦待機：兩份每一幀完全一樣
            var failed = false;
            try
            {
                foreach (var (yaw, pitch) in new[] { (0f, 20f), (45f, 30f), (-45f, 10f), (-60f, 35f), (90f, 20f), (30f, -40f) })
                {
                    var plain = Stage3DRig.Spawn(BlueAgent.MR.MrShell.Character, Vector3.zero, 0, BlueAgent.MR.MrShell.Height);
                    var looking = Stage3DRig.Spawn(BlueAgent.MR.MrShell.Character, new Vector3(5f, 0f, 0f), 0, BlueAgent.MR.MrShell.Height);
                    var facing = Vector3.ProjectOnPlane(looking.Forward, Vector3.up).normalized;
                    var head = looking.HeadBone.position;
                    var direction = Quaternion.AngleAxis(yaw, Vector3.up) * facing;
                    var right = Vector3.Cross(Vector3.up, direction).normalized;
                    looking.LookTarget = head + Quaternion.AngleAxis(-pitch, right) * direction;
                    const float dt = 1f / 30f;
                    for (var frame = 0; frame < 90; frame++)
                    {
                        plain.Animate(frame * dt, dt, 0f, 0f, 0f);
                        looking.Animate(frame * dt, dt, 0f, 0f, 0f);
                    }
                    var delta = looking.HeadBone.rotation * Quaternion.Inverse(plain.HeadBone.rotation);
                    var pointed = delta * facing;
                    var flat = Vector3.ProjectOnPlane(pointed, Vector3.up);
                    var gotYaw = Vector3.SignedAngle(facing, flat, Vector3.up);
                    var gotPitch = Mathf.Atan2(pointed.y, flat.magnitude) * Mathf.Rad2Deg;
                    var wantYaw = Mathf.Clamp(yaw, -Stage3DRig.LookYawLimit, Stage3DRig.LookYawLimit);
                    var wantPitch = Mathf.Clamp(pitch, -25f, 40f);
                    var ok = Mathf.Abs(gotYaw - wantYaw) < 3f && Mathf.Abs(gotPitch - wantPitch) < 3f;
                    failed |= !ok;
                    Debug.Log($"[mr] look target yaw={yaw:F0} pitch={pitch:F0} ⇒ head yaw={gotYaw:F1} pitch={gotPitch:F1} "
                              + $"(want {wantYaw:F0}/{wantPitch:F0}) ⇒ {(ok ? "OK" : "NG")}");
                    plain.Dispose();
                    looking.Dispose();
                }

                // 甩頭：先看左 60°，目標一下跳到右 60°（你從她背後繞過去）⇒ 頭相對身體的角速度不得超過上限＋待機那一點
                // 正上方：目標移到她頭頂正上方 ⇒ 左右角不該動（只追上下）
                var rig = Stage3DRig.Spawn(BlueAgent.MR.MrShell.Character, Vector3.zero, 0, BlueAgent.MR.MrShell.Height);
                var front = Vector3.ProjectOnPlane(rig.Forward, Vector3.up).normalized;
                Vector3 Aim(float degrees) => rig.HeadBone.position + Quaternion.AngleAxis(degrees, Vector3.up) * front;
                const float step = 1f / 72f;
                var time = 0f;
                rig.LookTarget = Aim(60f);
                for (var frame = 0; frame < 216; frame++, time += step) rig.Animate(time, step, 0f, 0f, 0f);
                rig.LookTarget = Aim(-60f);
                var last = Quaternion.Inverse(rig.Root.transform.rotation) * rig.HeadBone.rotation;
                var fastest = 0f;
                for (var frame = 0; frame < 216; frame++, time += step)
                {
                    rig.Animate(time, step, 0f, 0f, 0f);
                    var now = Quaternion.Inverse(rig.Root.transform.rotation) * rig.HeadBone.rotation;
                    fastest = Mathf.Max(fastest, Quaternion.Angle(last, now) / step);
                    last = now;
                }
                var swingOk = fastest < 165f && Mathf.Abs(rig.LookYaw + 60f) < 3f;
                var yawBefore = rig.LookYaw;
                rig.LookTarget = rig.HeadBone.position + Vector3.up * 0.5f + front * 0.05f;
                for (var frame = 0; frame < 144; frame++, time += step) rig.Animate(time, step, 0f, 0f, 0f);
                var overheadOk = Mathf.Abs(rig.LookYaw - yawBefore) < 1f && rig.LookPitch > 35f;
                failed |= !swingOk || !overheadOk;
                Debug.Log($"[mr] look swing +60→-60：最快 {fastest:F0}°/s（上限 150）、停在 {rig.LookYaw:F1}° ⇒ {(swingOk ? "OK" : "NG")}");
                Debug.Log($"[mr] look overhead：左右 {yawBefore:F1}→{rig.LookYaw:F1}°、上下 {rig.LookPitch:F1}° ⇒ {(overheadOk ? "OK" : "NG")}");
                rig.Dispose();
            }
            finally
            {
                NextConfig.IdleMotion = idleWas;
            }
            EditorApplication.Exit(failed ? 1 : 0);
        }
    }
}
