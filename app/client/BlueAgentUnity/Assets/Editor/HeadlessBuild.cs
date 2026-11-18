using System;
using System.IO;
using System.Linq;
using BlueAgent.Logic;
using UnityEditor;
using UnityEditor.Build;         // NamedBuildTarget
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;             // GraphicsSettings（指派 URP）
using UnityEngine.Rendering.Universal;   // UniversalRenderPipelineAsset／UniversalRendererData
using UnityEngine.SceneManagement;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// 無頭建置（給 NAS 上的 script 叫）：`Unity -batchmode -quit -projectPath … -executeMethod …`
    ///
    /// 為什麼要這一支：這條線的驗收一向在 NAS 上跑（Mac 只是建置工作區），
    /// Unity 的 GUI 不進那條流程 ⇒ 模型匯入、場景、PlayerSettings、Build 全部用程式碼做，指令可重跑。
    /// </summary>
    public static class HeadlessBuild
    {
        /// <summary>與現役 app（`dev.deepsea.blueagent`）**刻意不同** ⇒ spike 不會蓋掉他手機上的正式版。</summary>
        private const string BundleId = "dev.deepsea.blueagent.unity";

        /// <summary>
        /// 指派 URP（Universal Render Pipeline）：介面是世界空間畫布、由相機畫；3D 立繪（`NextStage3D`）
        /// 的卡通著色也是 URP shader。renderer 用 URP 自己的預設 Universal Renderer（2026-10-30 起；
        /// 先前借的是 Live2D SDK 附的 renderer，Live2D 支援已移除）。
        ///
        /// ⚠️ renderer **要存成資產**（`AssetDatabase.CreateAsset`）：只 `CreateInstance` 的話它只活在記憶體，
        /// 管線資產存檔後引用斷掉 ⇒ 下次開專案是「沒有 renderer 的 URP」，畫面只剩底色。
        /// </summary>
        internal static void EnsureRenderPipeline()
        {
            const string settingsDir = "Assets/Settings";
            const string pipelinePath = settingsDir + "/BlueAgentUniversalRP.asset";
            const string rendererPath = settingsDir + "/BlueAgentUniversalRenderer.asset";

            // 每次都重建：設定改了、或先前借 SDK renderer 建的舊資產，都不會留下來。
            if (AssetDatabase.LoadAssetAtPath<UniversalRenderPipelineAsset>(pipelinePath) != null)
                AssetDatabase.DeleteAsset(pipelinePath);
            if (AssetDatabase.LoadAssetAtPath<ScriptableRendererData>(rendererPath) != null)
                AssetDatabase.DeleteAsset(rendererPath);
            Directory.CreateDirectory(settingsDir);

            var rendererData = CreateRendererAsset(rendererPath);
            var pipeline = UniversalRenderPipelineAsset.Create(rendererData);
            // **4× MSAA**（2026-10-19，owner：「曲線元素的鋸齒感很強」）：介面是世界空間畫布、由相機畫，
            // 旋轉過的幾何（人字紋 ‹ ›、斜放的矩形）邊緣只能靠多重取樣抗鋸齒；預設 1（＝關）。
            // 行動 GPU（TBDR）上 MSAA 在 tile 內解析，成本很低。
            pipeline.msaaSampleCount = 4;
            AssetDatabase.CreateAsset(pipeline, pipelinePath);
            AssetDatabase.SaveAssets();

            GraphicsSettings.defaultRenderPipeline = pipeline;
            QualitySettings.renderPipeline = pipeline;
            Debug.Log($"[spike] 渲染管線：{pipeline.name}（URP；renderer={rendererData.name}）");
        }

        /// <summary>
        /// 建一份 URP 預設的 Universal Renderer 資產。走 URP 自己「Create → Rendering → URP Universal Renderer」
        /// 那一支（`UniversalRenderPipelineAsset.CreateRendererAsset`，internal ⇒ 反射），它會把後處理資料等
        /// 預設資源一起填好；這個 URP 版本找不到那一支時退回 `CreateInstance` 自己存。
        /// </summary>
        private static ScriptableRendererData CreateRendererAsset(string path)
        {
            var method = typeof(UniversalRenderPipelineAsset).GetMethod("CreateRendererAsset",
                System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
            if (method != null && method.GetParameters().Length == 4)
            {
                var created = method.Invoke(null, new object[] { path, RendererType.UniversalRenderer, false, "Renderer" })
                    as ScriptableRendererData;
                if (created != null) return created;
            }
            Debug.LogWarning("[spike] 找不到 URP 的 CreateRendererAsset ⇒ 自己建（沒有預設後處理資料）");
            var data = ScriptableObject.CreateInstance<UniversalRendererData>();
            AssetDatabase.CreateAsset(data, path);
            return data;
        }

        /// <summary>
        /// **准許明文 HTTP**（`PlayerSettings.insecureHttpOption`）。橋是區網上的純 HTTP
        /// （`http://100.64.0.1:8900`）——Unity 6 **預設拒絕**這種連線，症狀是
        /// `UnityWebRequest` 當場失敗、訊息只有一句 `Insecure connection not allowed`
        /// （HTTP code 0，看起來像「連不上」，其實根本沒送出去）。實測就在在線探針第一次跑時踩到。
        ///
        /// 這是**專案設定**（寫進 `ProjectSettings/ProjectSettings.asset`）⇒ 建置前設一次就對之後的
        /// 產物都有效；也因為是設定而不是場景裡的東西，`CreateSpikeScene` 不做這件事。
        /// ⚠️ **Android 那一側還有一層**：Android 9+ 的明文政策（`usesCleartextTraffic`／
        /// network-security-config）——真機上要連區網的橋，這條要另外處理（見 README 的未驗清單）。
        /// </summary>
        public static void EnsureHttpAllowed()
        {
            if (PlayerSettings.insecureHttpOption == InsecureHttpOption.AlwaysAllowed)
            {
                Debug.Log("[http] 已准許明文 HTTP（insecureHttpOption=AlwaysAllowed）");
                return;
            }
            PlayerSettings.insecureHttpOption = InsecureHttpOption.AlwaysAllowed;
            Debug.Log("[http] 設定專案准許明文 HTTP（insecureHttpOption ⇒ AlwaysAllowed；橋是區網的純 HTTP）");
            AssetDatabase.SaveAssets();
        }

        /// <summary>
        /// **把執行期才 `Shader.Find` 的 shader 釘進「Always Included Shaders」**（2026-10-14 真機實測的治本）。
        ///
        /// 症狀：她的回覆**含貼圖**（`… :031_hello_owner`）時，對話屏那一層**整屏建不起來**——
        /// log 是 `[screen] 貼圖 sprite asset 就緒：… 材質 **找不到 shader**` ⇒ `spriteAsset.material`
        /// 是 null ⇒ TMP 在 `ValidateHtmlTag` → `AddMaterialReference(null, …)` 直接
        /// `NullReferenceException`（`[ui] 疊層失敗（talk）`，畫面只剩殘影）。
        ///
        /// 成因與 URP 那條同一個：**建置會把「沒有任何資產引用」的 shader 裁掉**，而
        /// `TextMeshPro/Sprite` 在專案裡沒有材質引用它（貼圖那條路是程式建的 Material）。
        /// ⇒ 走 PlayerSettings 的「Always Included Shaders」是**建置前**就要生效的作法。
        ///
        /// ⚠️ 另一邊也做了防護（`UiBuilder.Text`：材質生不出來時退成純文字）——這一支治本，
        /// 那一支保證「一屏建不起來不連累整個 app」。
        /// </summary>
        /// <param name="extra">再多釘幾支（MR 的接地陰影、軟遮擋預處理；手機包不帶）。</param>
        public static void EnsureAlwaysIncludedShaders(params string[] extra)
        {
            // TMP 的 sprite（貼圖那條）＋ 3D 立繪的卡通著色（`NextStage3D` 執行期 `Shader.Find`，
            // 專案裡沒有材質資產引用它 ⇒ 不釘的話建置會裁掉，切到 3D 就退回 2D）。
            var assets = AssetDatabase.LoadAllAssetsAtPath("ProjectSettings/GraphicsSettings.asset");
            if (assets == null || assets.Length == 0)
            {
                Debug.LogWarning("[shader] 讀不到 GraphicsSettings.asset ⇒ 這一條跳過");
                return;
            }
            var serialized = new SerializedObject(assets[0]);
            var list = serialized.FindProperty("m_AlwaysIncludedShaders");
            if (list == null || !list.isArray)
            {
                Debug.LogWarning("[shader] GraphicsSettings 沒有 m_AlwaysIncludedShaders ⇒ 這一條跳過");
                return;
            }
            var changed = false;
            var names = new System.Collections.Generic.List<string> { "TextMeshPro/Sprite", BlueAgent.Next.Stage3DRig.ToonShader };
            names.AddRange(extra);
            foreach (var shaderName in names)
            {
                var shader = Shader.Find(shaderName);
                if (shader == null)
                {
                    Debug.LogWarning($"[shader] 找不到 {shaderName} ⇒ 這一條跳過");
                    continue;
                }
                var present = false;
                for (var i = 0; i < list.arraySize && !present; i++)
                {
                    present = list.GetArrayElementAtIndex(i).objectReferenceValue == shader;
                }
                if (present) continue;
                list.InsertArrayElementAtIndex(list.arraySize);
                list.GetArrayElementAtIndex(list.arraySize - 1).objectReferenceValue = shader;
                changed = true;
                Debug.Log($"[shader] 把 {shaderName} 釘進 Always Included Shaders（不然建置會把它裁掉）");
            }
            if (!changed) return;
            serialized.ApplyModifiedProperties();
            AssetDatabase.SaveAssets();
        }

        /// <summary>
        /// 把相機畫面渲進檔案（走**渲請求**：URP 下 `Camera.Render()` 不能用）。
        ///
        /// ⚠️ **每次拍都用新的 RenderTexture**：實測同一張重複當渲請求的目的地時，
        ///    第二次之後會拿到「只有底色」的圖（RenderGraph 會回收它經手過的目標紋理）。
        /// ⚠️ **一次運行只拍得成一張**：實測同一輪第二次 `SubmitRenderRequest` 拿到的也只有底色
        ///    ⇒ 要多張就跑多次。
        /// </summary>
        internal static bool CaptureTo(Camera camera, string path, int width, int height)
        {
            var target = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
            var request = new RenderPipeline.StandardRequest { destination = target };
            if (!RenderPipeline.SupportsRenderRequest(camera, request))
            {
                Debug.LogError("[stage3d] 這個管線不支援渲請求 ⇒ 拍不了");
                target.Release();
                UnityEngine.Object.DestroyImmediate(target);
                return false;
            }
            RenderPipeline.SubmitRenderRequest(camera, request);

            RenderTexture.active = target;
            var texture = new Texture2D(width, height, TextureFormat.RGBA32, false);
            texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            texture.Apply();
            RenderTexture.active = null;

            File.WriteAllBytes(path, texture.EncodeToPNG());
            Debug.Log($"[stage3d] 預覽圖：{path}（{width}x{height}）");

            target.Release();
            UnityEngine.Object.DestroyImmediate(target);
            UnityEngine.Object.DestroyImmediate(texture);
            return true;
        }

        /// <summary>
        /// Android 的**固定签名金钥**（`BlueAgentUnity/Keys/blueagent-dev.keystore`）。
        ///
        /// 为什么要有这支（owner 2026-10-20 指示「配固定 keystore」）：`AndroidKeystoreName` 留空时
        /// Unity 用**各台建置机自己的** `~/.android/debug.keystore` —— mini 与 Air 那两把证书不同
        /// （实测 `04ad3fd6…` / `278ecdc2…`）⇒ 换机出包就 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，
        /// 只能先卸载（丢掉手机上的桥位址／角色／历史）。有了固定金钥，任一台机器出的包都能
        /// `install -r` 直接升级。
        ///
        /// ⚠️ 这支 keystore 是 **Air 上原本那把 debug key**（证书 SHA-256 `278ecdc2…`），
        /// 所以现役手机上的旧包**能继续覆盖升级**；换一把新 key 就等于要卸载重装。
        /// 密码是 debug keystore 的公开预设（`android`／alias `androiddebugkey`），
        /// 可用环境变数 `BA_KS_PASS`／`BA_KA_PASS` 覆写。
        /// </summary>
        internal static void ConfigureAndroidSigning()
        {
            var keyPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "Keys", "blueagent-dev.keystore"));
            if (!File.Exists(keyPath))
            {
                Debug.LogWarning($"[spike] ⚠️ 找不到固定金钥 {keyPath} ⇒ 退回各机 debug keystore（换机出包签章会变）");
                return;
            }
            PlayerSettings.Android.useCustomKeystore = true;
            PlayerSettings.Android.keystoreName = keyPath;
            PlayerSettings.Android.keystorePass = Environment.GetEnvironmentVariable("BA_KS_PASS") ?? "android";
            PlayerSettings.Android.keyaliasName = "androiddebugkey";
            PlayerSettings.Android.keyaliasPass = Environment.GetEnvironmentVariable("BA_KA_PASS") ?? "android";
            Debug.Log($"[spike] Android 签名：固定金钥 {keyPath}（alias androiddebugkey）");
        }

        /// <summary>
        /// Next：包裡宣告「直＋橫都支援」，執行期再按螢幕大小收（`NextLayout.ApplyOrientation`：
        /// 只有大螢幕放開橫向，手機照舊直向）。owner 2026-10-30「iPhone Duo 的展開屏幕適配」：
        /// 展開的內屏天生是橫的，包裡只宣告直向 ⇒ 系統把 app 縮成中間一條、兩邊黑邊。
        /// </summary>
        private static void AllowLargeScreenRotation()
        {
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.AutoRotation;
            PlayerSettings.allowedAutorotateToPortrait = true;
            PlayerSettings.allowedAutorotateToPortraitUpsideDown = false;
            PlayerSettings.allowedAutorotateToLandscapeLeft = true;
            PlayerSettings.allowedAutorotateToLandscapeRight = true;
        }

        /// <summary>iOS 的 PlayerSettings（只在 iOS 那一支呼叫）。</summary>
        private static void ConfigureIOS()
        {
            var named = NamedBuildTarget.iOS;
            PlayerSettings.SetApplicationIdentifier(named, BundleId);
            PlayerSettings.SetScriptingBackend(named, ScriptingImplementation.IL2CPP);
            PlayerSettings.SetManagedStrippingLevel(named, ManagedStrippingLevel.Minimal);

            // iPhone＋iPad（owner 2026-11-01「沒適配平板寬螢幕」）：只宣告 iPhone 時 iPadOS 用相容模式開
            // （iPhone 大小的小窗），版面量不到真的螢幕。大螢幕那一套（`NextLayout`：短邊 ≥ 600 pt 放開橫向、
            // 1 設計單位≈1 pt、≥ 760 分兩欄）本來就有，iPad 照著走；`UIRequiresFullScreen` 已開，不必支援分割畫面。
            PlayerSettings.iOS.targetDevice = iOSTargetDevice.iPhoneAndiPad;
            PlayerSettings.iOS.targetOSVersionString = "15.0";
            // 手機端是豎屏單欄（與 Android 同一份版面）
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.Portrait;
            PlayerSettings.allowedAutorotateToPortrait = true;
            PlayerSettings.allowedAutorotateToPortraitUpsideDown = false;
            PlayerSettings.allowedAutorotateToLandscapeLeft = false;
            PlayerSettings.allowedAutorotateToLandscapeRight = false;

            var sim = Environment.GetEnvironmentVariable("BA_IOS_SIM") == "1";
            PlayerSettings.iOS.sdkVersion = sim ? iOSSdkVersion.SimulatorSDK : iOSSdkVersion.DeviceSDK;
            if (sim) SetSimulatorArchArm64();

            // 橋是區網純 HTTP ⇒ ATS 要放行（等於 NSAllowsArbitraryLoads）
            PlayerSettings.iOS.allowHTTPDownload = true;
            // 權限說明字串：沒填的話 iOS 會**直接殺掉**要權限的那一幀
            PlayerSettings.iOS.microphoneUsageDescription = "BlueAgent 要用麥克風聽你呼喚她（收音只送到你自己設定的橋）。";
            PlayerSettings.iOS.cameraUsageDescription = "BlueAgent 只在你要她看畫面時用相機。";
            PlayerSettings.iOS.locationUsageDescription = "BlueAgent 只用大致位置顯示天氣。";

            var team = Environment.GetEnvironmentVariable("BA_IOS_TEAM");
            if (!string.IsNullOrEmpty(team))
            {
                PlayerSettings.iOS.appleDeveloperTeamID = team;
                PlayerSettings.iOS.appleEnableAutomaticSigning = true;
            }

            // App 图标（owner 2026-10-20 给的新标志，与 Android 同一张源图烤出来）。
            // 这支 1024×1024 是**不透明** PNG（Apple 不收带 alpha 的）；iOS 会自己切圆角。
            // 没设过的话 iOS 用 Unity 预设图标（就是「没有 logo」那个观感）。
            var iconPath = "Assets/Icons/appicon_ios_1024.png";
            var icon = AssetDatabase.LoadAssetAtPath<Texture2D>(iconPath);
            if (icon != null)
            {
                PlayerSettings.SetIcons(NamedBuildTarget.iOS, new[] { icon }, IconKind.Application);
                Debug.Log($"[ios] app icon：{iconPath}（{icon.width}x{icon.height}）");
            }
            else
            {
                Debug.LogWarning($"[ios] ⚠️ 找不到 {iconPath} ⇒ 用 Unity 预设图标（桌面会没有 logo）");
            }

            Debug.Log($"[ios] 切片={(sim ? "模擬器 arm64" : "device")}／bundle={BundleId}／" +
                      $"team={(string.IsNullOrEmpty(team) ? "（未指定，模擬器不需要）" : team)}");
        }

        /// <summary>
        /// 模擬器切片要 arm64（Apple Silicon 原生）。
        ///
        /// ⚠️ 這裡**刻意用反射**：這個屬性在各版 Unity 的型別名不一樣（`iOSSimulatorArchitecture` 在某些版本
        /// 不在 `UnityEditor` 命名空間／或整支被改掉），直接寫死的名字會在別台機器上編不過（2026-10-19 實測：
        /// `error CS0103: The name 'iOSSimulatorArchitecture' does not exist`）。
        /// 找不到屬性時**不報錯**——預設值照樣能出包，只是可能落到 x86_64 切片（Rosetta 可跑）。
        /// </summary>
        private static void SetSimulatorArchArm64()
        {
            const string name = "simulatorSdkArchitecture";
            var prop = typeof(PlayerSettings.iOS).GetProperty(name,
                System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
            if (prop == null)
            {
                Debug.LogWarning($"[ios] 這個 Unity 沒有 PlayerSettings.iOS.{name} ⇒ 用預設切片");
                return;
            }
            try
            {
                var value = Enum.Parse(prop.PropertyType, "ARM64");
                prop.SetValue(null, value);
                Debug.Log($"[ios] 模擬器切片設為 {value}（{prop.PropertyType.Name}）");
            }
            catch (Exception e)
            {
                Debug.LogWarning($"[ios] 設 {name} 失敗（{e.GetType().Name}: {e.Message}）⇒ 用預設切片");
            }
        }

        // ══════════════════════════════════════════════════════════════════════════
        // blueagent-next 的雙端（master／guest）——2026-10-22 新增
        //
        // 舊殼（Spike：AppShell／BridgeShell）的場景與出包入口 2026-10-31 已移除（owner：「旧壳退役」）；
        // 這裡只剩 Next：場景裡只掛 `NextShell`（它自己建畫布與全部 UI）。
        // 契約見倉庫根目录 `docs/accounts-and-clients.md`＋`docs/APP-API-SSE.md`。
        // ══════════════════════════════════════════════════════════════════════════

        private const string NextScenePath = "Assets/Scenes/Next.unity";
        /// <summary>與現役包、Spike 包都不同 ⇒ 兩個 app 能同時裝在手機上（不會互蓋／互搶 session）。</summary>
        private const string NextBundleId = "dev.deepsea.blueagent.next";

        /// <summary>Next 的場景：只有一台相機＋`NextShell`＋`EventSystem`（畫布與所有屏由 NextShell 建）。</summary>
        [MenuItem("BlueAgent/Next シーンを作り直す")]
        public static void CreateNextScene()
        {
            Directory.CreateDirectory("Assets/Scenes");
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            var cameraGo = new GameObject("Main Camera", typeof(Camera));
            var camera = cameraGo.GetComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.933f, 0.965f, 0.992f);   // eef6fd（NextUi.PageBg）
            camera.orthographic = true;
            camera.orthographicSize = 5f;
            camera.nearClipPlane = 0.1f;
            camera.farClipPlane = 100f;
            cameraGo.tag = "MainCamera";
            // 沒有它 ⇒ AudioSource 照樣播、就是沒有聲音（同 Spike 那一條的坑）
            cameraGo.AddComponent<AudioListener>();
            cameraGo.AddComponent<BlueAgent.Next.NextShell>();

            if (UnityEngine.Object.FindFirstObjectByType<UnityEngine.EventSystems.EventSystem>() == null)
            {
                var eventSystem = new GameObject("EventSystem",
                                                 typeof(UnityEngine.EventSystems.EventSystem),
                                                 typeof(UnityEngine.EventSystems.StandaloneInputModule));
                eventSystem.transform.SetParent(cameraGo.transform, false);
            }

            EditorSceneManager.SaveScene(scene, NextScenePath);
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(NextScenePath, true) };
            Debug.Log($"[next] 場景好了：{NextScenePath}（只掛 NextShell）");
        }

        public static void BuildNextAndroid() =>
            BuildNext(BuildTarget.Android, "BA_NEXT_APK_OUT", "Builds/BlueAgentNext.apk");

        public static void BuildNextMac() =>
            BuildNext(BuildTarget.StandaloneOSX, "BA_NEXT_MAC_OUT", "Builds/BlueAgentNext.app");

        /// <summary>Next 的 iOS（出 Xcode 專案；`BA_IOS_SIM=1` 出模擬器切片）。</summary>
        public static void BuildNextIOS() =>
            BuildNext(BuildTarget.iOS, "BA_NEXT_IOS_OUT", "Builds/next-ios");

        private static void BuildNext(BuildTarget target, string envVar, string defaultPath)
        {
            EnsureRenderPipeline();
            EnsureHttpAllowed();              // 區網是純 HTTP（Unity 6 預設拒收）
            EnsureAlwaysIncludedShaders();
            CreateNextScene();

            PlayerSettings.companyName = "BlueAgent";
            PlayerSettings.productName = "BlueAgent Next";

            var named = NamedBuildTarget.FromBuildTargetGroup(BuildPipeline.GetBuildTargetGroup(target));
            var outPath = Environment.GetEnvironmentVariable(envVar);
            if (string.IsNullOrEmpty(outPath)) outPath = Path.GetFullPath(defaultPath);
            Directory.CreateDirectory(Path.GetDirectoryName(outPath));

            // 驗收包要**可除錯**：`BA_SELFTEST=1` 時打開 Development Build ⇒ APK 是 debuggable，
            // `adb shell run-as <pkg>` 才進得去（模擬器沒有麥克風，注入 wav 要靠 run-as 寫檔）。
            var selfTestBuild = Environment.GetEnvironmentVariable("BA_SELFTEST") == "1";
            EditorUserBuildSettings.development = selfTestBuild;
            EditorUserBuildSettings.allowDebugging = selfTestBuild;

            if (target == BuildTarget.Android)
            {
                PlayerSettings.SetApplicationIdentifier(named, NextBundleId);
                PlayerSettings.Android.forceInternetPermission = true;
                PlayerSettings.SetScriptingBackend(named, ScriptingImplementation.IL2CPP);
                PlayerSettings.Android.targetArchitectures = AndroidArchitecture.ARM64;
                PlayerSettings.Android.minSdkVersion = AndroidSdkVersions.AndroidApiLevel26;
                PlayerSettings.SetManagedStrippingLevel(named, ManagedStrippingLevel.Minimal);
                EditorUserBuildSettings.buildAppBundle = false;
                ConfigureAndroidSigning();
            }
            else if (target == BuildTarget.iOS)
            {
                ConfigureIOS();                                    // 舊殼那一套（模擬器切片、ATS、權限字串、圖示）
                PlayerSettings.SetApplicationIdentifier(named, NextBundleId);
            }
            else
            {
                PlayerSettings.fullScreenMode = FullScreenMode.Windowed;
                PlayerSettings.defaultScreenWidth = 720;
                PlayerSettings.defaultScreenHeight = 1280;
            }
            // ⚠️ 要放在 `ConfigureIOS()` **之後**：它是舊殼那一套，會把方向鎖回直向。
            AllowLargeScreenRotation();

            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { NextScenePath },
                locationPathName = outPath,
                target = target,
                // ⚠️ 只有 `EditorUserBuildSettings.development` 不夠——`run-as` 要的是 APK 本身
                // debuggable，那由 BuildOptions.Development 決定（踩過：只設 Editor 開關，run-as 照樣拒絕）。
                options = selfTestBuild ? BuildOptions.Development : BuildOptions.None,
                // 驗收鉤子（`BA_SELFTEST=1` 才編進去）：目前只有 `NextVoice` 的「wav 注入」——
                // 模擬器沒有麥克風輸入，靠它驗 `/api/audio` 那條路；正式包不該有。
                extraScriptingDefines = Environment.GetEnvironmentVariable("BA_SELFTEST") == "1"
                    ? new[] { "BA_SELFTEST" } : new string[0],
            });
            Debug.Log($"[next] 驗收鉤子 BA_SELFTEST={(selfTestBuild ? "ON" : "off")}"
                      + $"／debuggable={(selfTestBuild ? "on" : "off")}");
            Debug.Log($"[next] Build result={report.summary.result} size={report.summary.totalSize} " +
                      $"errors={report.summary.totalErrors} ⇒ {outPath}");
            if (report.summary.result != BuildResult.Succeeded) EditorApplication.Exit(2);
        }
    }
}
