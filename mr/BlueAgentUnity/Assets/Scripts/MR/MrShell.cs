using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using BlueAgent.Next;
using Unity.XR.CoreUtils;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.XR;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;
// core-utils 也有一個 SerializableGuid；錨點 API 用的是 ARSubsystems 這一個
using SerializableGuid = UnityEngine.XR.ARSubsystems.SerializableGuid;
#if UNITY_ANDROID && !UNITY_EDITOR
using UnityEngine.Android;
#endif

namespace BlueAgent.MR
{
    /// <summary>
    /// Quest 3 混合現實端的殼（`Assets/Scenes/MR.unity` 裡唯一的東西；PLAN-mr-quest3）。
    ///
    /// M0：passthrough 裡，把現役一名角色按真實尺度**站在量出來的地面上**，驗收數字畫在眼前、也打進 logcat。
    /// M1：
    /// <list type="bullet">
    /// <item>**釘住**：她站的那一點存成持久錨點（Meta 的 spatial anchor），下次開 app 先找回那一點、站回原位；
    ///   找不到（換了房間、錨點被清）就照 M0 放在你面前、重新存。重放＝手柄 B／Y。</item>
    /// <item>**遮擋**：環境深度（AROcclusionManager ＋ ARShaderOcclusion，軟遮擋）→ 她的著色器把被真實物體擋住的像素
    ///   alpha 降成 0。手柄 A／X 開關（量「遮擋前後幀率差」用）。</item>
    /// <item>**接地陰影**：`MrGroundShadow`。</item>
    /// </list>
    /// §5.1（owner 2026-11-01）：**桌上 30 cm、固定位置**——自動找你前方最近的桌面放她；沒有桌子就站地上並明示。
    ///
    /// 跟 `NextShell` 同一個作法：場景裡只掛這一支，XR Origin、AR 管理器、相機全部**執行期組裝**
    /// （專案不進版控 `.meta`，場景由 `MrBuild.CreateMrScene` 現場生）。
    /// 走 OpenXR ＋ AR Foundation ＋ Unity OpenXR Meta，不引 Meta XR SDK（PLAN §4-3）。
    /// </summary>
    public sealed class MrShell : MonoBehaviour
    {
        /// <summary>第一次召喚誰（PLAN §3 M0：mira）；之後左手雙擊換過的人記在 <see cref="KeyCharacter"/>。</summary>
        public const string Character = "mira";
        /// <summary>上次站在那裡的是誰（換人不動錨點 ⇒ 重開 app 時同一個點上站的應該是同一位）。</summary>
        private const string KeyCharacter = "ba_mr_character";

        /// <summary>
        /// 她的身高（公尺，含光環）。owner §5-1（2026-11-01）：**桌上 30 cm**（M0／M1 驗收時是 1:1 的 1.45 m）。
        /// </summary>
        public const float Height = 0.30f;

        /// <summary>軟遮擋的深度預處理（AR Foundation 附的；`MrBuild` 把它釘進 Always Included）。</summary>
        public const string SoftOcclusionShader = "Occlusion/Soft/DepthPreprocessing";

        /// <summary>桌子離你最遠多少還算「你前方的桌子」（水平距離，公尺）。</summary>
        private const float MaxTableDistance = 2.5f;
        /// <summary>她離你這一側的桌邊至少多遠（公尺）：不要站在桌邊上。</summary>
        private const float EdgeMargin = 0.12f;
        /// <summary>檢查站位時從桌面往上多高打下來（層板之類擋在她頭上的不行）。</summary>
        private const float TableClearance = 0.6f;
        /// <summary>沒有桌子、退回站地上時：離你多遠，從多高打下來（桌子擋著不行）。</summary>
        private const float FloorDistance = 1.0f;
        private const float FloorClearance = 2f;

        /// <summary>等桌面（平面）最多幾秒；超過就站地上或推定的地面並明示原因。</summary>
        private const float SurfaceTimeout = 8f;

        /// <summary>載回來的錨點最多等幾秒「重新定位」（追蹤狀態變成 Tracking）；等不到＝不在同一個房間。</summary>
        private const float RelocalizeTimeout = 5f;

        /// <summary>
        /// 讀 Space Setup 資料（平面、包圍盒、網格、環境深度）要的 Android 權限。Unity OpenXR Meta 會自己把它寫進 manifest，
        /// 但**不會替 app 申請**（套件 CHANGELOG：OpenXR 1.11 起要 app 自己要）⇒ 沒要到就一個平面都拿不到。
        /// </summary>
        private const string ScenePermission = "com.oculus.permission.USE_SCENE";

        /// <summary>存下來的錨點 GUID（桌上版；M0／M1 那個站地上的 1.45 m 錨點用舊 key，啟動時丟掉）。</summary>
        private const string KeyAnchor = "ba_mr_anchor_table";
        /// <summary>存錨點時她在**所站平面座標系**裡的位置（`平面 id|x|y|z`）：重開後拿來量「回到原位差多少」。</summary>
        private const string KeyAnchorSurface = "ba_mr_anchor_table_ref";
        private static readonly string[] OldKeys = { "ba_mr_anchor", "ba_mr_anchor_floor" };

        private XROrigin _origin;
        private Camera _camera;
        private ARPlaneManager _planes;
        private ARRaycastManager _raycasts;
        private ARAnchorManager _anchors;
        private AROcclusionManager _occlusion;
        private ARShaderOcclusion _shaderOcclusion;
        private MrProbe _probe;
        private MrGroundShadow _shadow;
        private Stage3DRig _rig;
        private ARPlane _surface;
        private ARAnchor _anchor;
        private float _surfaceY;
        private float _lift;
        private float _yaw;

        /// <summary>從什麼時候開始等地面（權限有結果才起算）。</summary>
        private float _waitFrom = -1f;
        // 權限回呼不保證在主執行緒 ⇒ 只記旗標，Update 裡處理
        private volatile bool _granted;
        private volatile bool _denied;

        /// <summary>錨點的非同步操作（載入／存／清）進行中：不要同時放第二個她。</summary>
        private bool _busy;
        private bool _triedLoad;
        private bool _measureRestore;
        /// <summary>
        /// 從錨點還原後，這麼久（秒）她腳下還是沒有任何平面 ⇒ 桌子被搬走／換了房間但錨點照樣 tracking，
        /// 她會懸空或陷進家具 ⇒ 不信這個錨點，重放到你前方最近的桌面並明示（評審 2026-11-01 P2-4）。
        /// </summary>
        private const float RestoreSurfaceWait = 5f;
        private float _restoredAt = -1f;
        /// <summary>這次重放的原因（i18n key）：存好新錨點時取代「已記住這個位置」那一句。</summary>
        private string _placeWhy;

        private bool _occlusionWanted = true;
        /// <summary>環境深度來過至少一幀。之前不開 ARShaderOcclusion：沒有深度圖時遮擋會把她整個藏起來。</summary>
        private bool _depthSeen;

        /// <summary>
        /// 你走到她旁邊多偏（相對她身體正前方，度）她就**轉身**（頭最多轉 <see cref="Stage3DRig.LookYawLimit"/>）；
        /// 轉到剩多少停、每秒轉多快。M2。
        /// </summary>
        private const float BodyTurnStart = 55f;
        private const float BodyTurnStop = 8f;
        private const float BodyTurnSpeed = 60f;
        private bool _turning;

        /// <summary>啟動後幾秒檢查一次 passthrough 那一層有沒有建起來（沒有＝背景全黑，要明示）。</summary>
        private const float PassthroughCheckAt = 5f;
        /// <summary>
        /// 查到這個時間（秒）還沒有才算「沒有」：慢啟動時 5 秒那一下層可能還沒建起來，只查一次會誤報（評審 2026-11-01 P3-8）。
        /// 中間每秒查一次，查到就停。
        /// </summary>
        private const float PassthroughGiveUpAt = 20f;
        private float _passthroughNextCheck;
        private bool _passthroughChecked;

        // ── M3：接橋、捏手指（捏住她可以拿起來放到別處）──
        // owner 2026-11-01：右手雙擊＝通話開／關（MrCall）、左手雙擊＝就地換人；「捏兩下說一句」下線
        /// <summary>
        /// 「捏兩下」＝第一下**放開**之後這麼久（秒）以內第二下捏下去。以前量的是兩次「捏下」的間隔（0.45 s），
        /// 但 owner 捏得慢（手指併著約 1 秒才放），兩下永遠湊不成、第一下還被當成長按召回
        /// （2026-11-01 實機：想說話，結果召回 4 次）⇒ 改成從放開起算，捏多久都不影響（只要短於長按）。
        /// </summary>
        private const float DoublePinch = 0.8f;
        /// <summary>捏點離她身體中線多近（公尺）算「捏住她」：她身邊的一圈抓取膠囊（腳到頭頂）。</summary>
        private const float GrabRadius = 0.1f;
        /// <summary>放手時往下找桌面／地面最多找多深（公尺）。</summary>
        private const float DropSearch = 2f;

        private Transform _trackingSpace;
        private MrBridge _bridge;
        private MrPinch _pinch;
        private MrCall _call;
        private bool _micAsked;
        private float _lastTapAt = -1f;
        /// <summary>上一下是哪隻手（雙擊要同一隻手：左右手意思不同）。</summary>
        private int _lastTapHand = -1;
        /// <summary>換人進行中（淡出 → 換模型 → 淡入）：這段時間不接第二次換人、不接召回。</summary>
        private bool _switching;
        private int _grabHand = -1;
        private Vector3 _grabOffset;
        private Vector3 _grabFrom;
        private float _think;
        private float _speak;
        // ── M4：召喚（owner 2026-11-01：空中捏住不放＝重新召喚；淡入淡出；不要一召喚就貼臉）──
        /// <summary>
        /// 空中（不在她身上）捏住多久算「重新召喚」（秒）。原本 1 秒，跟 owner 慢慢捏的一下（約 1 秒）撞在一起 ⇒ 2 秒
        /// （明顯是故意按住，不會跟「捏兩下」搞混）。
        /// </summary>
        private const float LongPinch = 2f;
        /// <summary>淡入／淡出各多久（秒）。</summary>
        private const float FadeSeconds = 0.4f;
        /// <summary>桌上的她離你（頭，水平）至少多遠（公尺）。</summary>
        private const float MinDistance = 0.45f;
        private int _holdHand = -1;
        private float _holdStart;
        private float _spawnedAt = -100f;
        private float _fadeOutAt = -1f;

        /// <summary>她說完之後氣泡還留幾秒（秒）。</summary>
        private const float BubbleLinger = 5f;
        private MrBubble _bubble;
        /// <summary>視野下方的事件通知（owner 2026-11-01：除錯儀表藏起來，事件改這裡說）。</summary>
        private MrToast _toast;
        /// <summary>通話中她頭頂的聽筒圖示（不在通話＝什麼都不顯示）。</summary>
        private MrCallBadge _callBadge;
        /// <summary>手部追蹤上一幀在不在跑（停了才說一次「看不到手」）。</summary>
        private bool _handsWere = true;
        private float _lastVoiceAt = -100f;
        private AudioSource _cue;
        private AudioClip _cueStart;
        private AudioClip _cueStop;

        private InputAction _toggleOcclusion;
        private InputAction _resummon;
        /// <summary>按下搖桿（左右手柄都算）＝除錯儀表開／關（預設藏起來，owner 2026-11-01）。</summary>
        private InputAction _toggleProbe;

        private readonly List<ARRaycastHit> _hits = new List<ARRaycastHit>();

        private void Awake()
        {
            // 舊的（站地上的）錨點不再用：只丟 key。頭顯裡那個空間錨點留著無害（沒人引用）
            foreach (var key in OldKeys) PlayerPrefs.DeleteKey(key);
            BuildRig();
            _probe = MrProbe.Create(_camera, _raycasts);
            _probe.StatusKey = "mr_hud_waiting";
            _shadow = MrGroundShadow.Create();

            var stored = PlayerPrefs.GetString(KeyCharacter, Character);
            _bridge = MrBridge.Create(NextStage3D.Has(stored) ? stored : Character);
            _bridge.Motion += clip => _rig?.PlayMotion(clip);
            _call = MrCall.Create(_bridge);
            _bubble = MrBubble.Create(_camera);
            _toast = MrToast.Create(_camera);
            _callBadge = MrCallBadge.Create(_camera);
            _bridge.Notice += _toast.Post;
            _call.Notice += _toast.Post;
            _pinch = MrPinch.Create(_trackingSpace);
            _pinch.Down += OnPinchDown;
            _pinch.Up += OnPinchUp;
            _cue = _camera.gameObject.AddComponent<AudioSource>();
            _cue.playOnAwake = false;
            _cue.spatialBlend = 0f;
            _cueStart = Beep("mr-cue-start", new[] { 660f, 990f });
            _cueStop = Beep("mr-cue-stop", new[] { 990f, 660f });

            // 兩隻手柄都算：A／X＝遮擋開關、B／Y＝重新召喚（M4 才做「點哪出哪」）
            _toggleOcclusion = new InputAction("Toggle Occlusion", binding: "<XRController>/primaryButton");
            _resummon = new InputAction("Resummon", binding: "<XRController>/secondaryButton");
            _toggleProbe = new InputAction("Toggle Debug Panel", binding: "<XRController>/thumbstickClicked");
            _toggleProbe.AddBinding("<XRController>/{Primary2DAxisClick}");
            _toggleOcclusion.Enable();
            _resummon.Enable();
            _toggleProbe.Enable();
        }

        private void Start()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (Permission.HasUserAuthorizedPermission(ScenePermission))
            {
                _granted = true;
            }
            else
            {
                var callbacks = new PermissionCallbacks();
                callbacks.PermissionGranted += _ => _granted = true;
                callbacks.PermissionDenied += _ => _denied = true;
                Permission.RequestUserPermission(ScenePermission, callbacks);
            }
#else
            _granted = true;
#endif
        }

        /// <summary>
        /// XR Origin（追蹤原點＝地面）→ Camera Offset → 主相機（頭顯姿勢、passthrough、環境深度）＋ 平面／射線／錨點管理器
        /// ＋ AR Session。⚠️ 整串先**關著**組裝再打開：`XROrigin`／`ARPlaneManager` 在 Awake／OnEnable 就要拿到相機與 Origin，
        /// `ARShaderOcclusion` 的軟遮擋設定也要在它 Awake 之前填好。
        /// </summary>
        private void BuildRig()
        {
            var root = new GameObject("XR Origin");
            root.SetActive(false);

            var offset = new GameObject("Camera Offset");
            _trackingSpace = offset.transform;
            offset.transform.SetParent(root.transform, false);

            var eye = new GameObject("Main Camera") { tag = "MainCamera" };
            eye.transform.SetParent(offset.transform, false);
            _camera = eye.AddComponent<Camera>();
            // passthrough：背景要**完全透明**（A＝0），合成層才透得出現實（ARCameraBackground 在 Quest 上無效、不掛）
            _camera.clearFlags = CameraClearFlags.SolidColor;
            _camera.backgroundColor = new Color(0f, 0f, 0f, 0f);
            _camera.nearClipPlane = 0.05f;
            _camera.farClipPlane = 30f;
            _camera.allowHDR = false;
            // 沒有它 ⇒ AudioSource 照樣播、就是沒有聲音（M3 才用得到，先掛上）
            eye.AddComponent<AudioListener>();

            var pose = eye.AddComponent<TrackedPoseDriver>();
            pose.positionInput = new InputActionProperty(
                new InputAction("Head Position", binding: "<XRHMD>/centerEyePosition", expectedControlType: "Vector3"));
            pose.rotationInput = new InputActionProperty(
                new InputAction("Head Rotation", binding: "<XRHMD>/centerEyeRotation", expectedControlType: "Quaternion"));
            pose.trackingStateInput = new InputActionProperty(
                new InputAction("Head Tracking State", binding: "<XRHMD>/trackingState", expectedControlType: "Integer"));
            eye.AddComponent<ARCameraManager>();                 // Unity OpenXR Meta：開著它＝開 passthrough

            _occlusion = eye.AddComponent<AROcclusionManager>();
            _occlusion.requestedEnvironmentDepthMode = EnvironmentDepthMode.Fastest;   // Meta 只有開／關
            _occlusion.enabled = false;                          // 環境深度也吃 USE_SCENE：權限到手才開
            _occlusion.frameReceived += OnDepthFrame;
            _shaderOcclusion = eye.AddComponent<ARShaderOcclusion>();
            UseSoftOcclusion(_shaderOcclusion);
            _shaderOcclusion.enabled = false;

            _origin = root.AddComponent<XROrigin>();
            _origin.Camera = _camera;
            _origin.CameraFloorOffsetObject = offset;
            _origin.RequestedTrackingOriginMode = XROrigin.TrackingOriginMode.Floor;

            _planes = root.AddComponent<ARPlaneManager>();
            _planes.requestedDetectionMode = PlaneDetectionMode.Horizontal;
            _planes.enabled = false;                             // 權限到手才開（沒權限開了也是空的）
            _raycasts = root.AddComponent<ARRaycastManager>();
            _anchors = root.AddComponent<ARAnchorManager>();

            root.SetActive(true);
            new GameObject("AR Session").AddComponent<ARSession>();
        }

        /// <summary>
        /// 軟遮擋（邊緣柔、比硬遮擋不假）要一支預處理 shader，但 `ARShaderOcclusion` 只讓編輯器序列化那一欄、
        /// 執行期加的元件填不進去 ⇒ 反射填（要在它 Awake 之前）。填不進去就退回預設的硬遮擋，照樣能遮。
        /// </summary>
        private static void UseSoftOcclusion(ARShaderOcclusion target)
        {
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            var shader = Shader.Find(SoftOcclusionShader);
            var shaderField = typeof(ARShaderOcclusion).GetField("m_SoftOcclusionPreprocessShader", flags);
            var modeField = typeof(ARShaderOcclusion).GetField("m_OcclusionShaderMode", flags);
            if (shader == null || shaderField == null || modeField == null)
            {
                Debug.LogWarning($"[mr] 軟遮擋設不起來（shader={(shader != null)}、欄位={(shaderField != null && modeField != null)}）⇒ 用硬遮擋");
                return;
            }
            shaderField.SetValue(target, shader);
            modeField.SetValue(target, AROcclusionShaderMode.SoftOcclusion);
        }

        private void Update()
        {
            if (_granted && !_planes.enabled)
            {
                _planes.enabled = true;
                _occlusion.enabled = _occlusionWanted;
                _waitFrom = Time.unscaledTime;
                Debug.Log("[mr] 空間資料權限：已授權 ⇒ 開平面追蹤與環境深度");
            }
            if (_denied && _waitFrom < 0f)
            {
                _waitFrom = Time.unscaledTime - SurfaceTimeout;   // 不會有平面了，不必再等
                Debug.LogWarning("[mr] 空間資料權限：被拒 ⇒ 拿不到地面平面與環境深度");
            }

            if (!_passthroughChecked && Time.unscaledTime > PassthroughCheckAt) CheckPassthrough();
            // 麥克風權限等空間資料那個對話框有結果再要（兩個同時跳，後一個會被吞掉）
            if (!_micAsked && (_granted || _denied))
            {
                _micAsked = true;
                _call.AskPermission();
            }
            _probe.Conversation = Conversation();
            // 手部追蹤停了（拿起手柄、手出了視野太久）：說一次，回來了不用說
            var hands = !_bridge.Ready || _pinch.Running;
            if (_handsWere && !hands) _toast.Post(NextStrings.Get("mr_hands_off"));
            _handsWere = hands;
            if (_holdHand >= 0)
            {
                if (!_pinch.Holding(_holdHand, out _)) _holdHand = -1;
                else if (Time.unscaledTime - _holdStart >= LongPinch)
                {
                    _holdHand = -1;
                    _lastTapAt = -1f;
                    _cue.PlayOneShot(_cueStart);
                    Debug.Log($"[mr] 空中捏住 {LongPinch:F0} 秒 ⇒ 重新召喚");
                    Resummon();
                }
            }
            if (_toggleOcclusion.WasPressedThisFrame()) ToggleOcclusion();
            if (_resummon.WasPressedThisFrame()) Resummon();
            if (_toggleProbe.WasPressedThisFrame())
            {
                _probe.Visible = !_probe.Visible;
                Debug.Log($"[mr] 除錯儀表：{(_probe.Visible ? "開" : "關")}");
            }

            if (_rig == null && _waitFrom >= 0f && !_busy)
            {
                if (!_triedLoad && _granted && PlayerPrefs.HasKey(KeyAnchor)) LoadSaved();
                else TryPlace();
            }
            if (_rig != null && _surface == null && _planes.enabled)
            {
                // 從錨點載回來時平面可能還沒到：到了再接上（儀表的漂移、還原誤差都靠它）
                _surface = SurfaceUnder(_rig.Root.transform.position - Vector3.up * _lift);
                if (_surface != null) _probe.Track(_rig, _surface, _surfaceY);
            }
            if (_measureRestore && _surface != null) MeasureRestore();
            if (_restoredAt > 0f)
            {
                if (_surface != null || _rig == null) _restoredAt = -1f;           // 腳下接到平面了（或她已經不在）
                else if (Time.unscaledTime - _restoredAt > RestoreSurfaceWait && !_busy)
                {
                    _restoredAt = -1f;
                    Debug.LogWarning($"[mr] 錨點還原後 {RestoreSurfaceWait:F0} s 她腳下沒有平面（桌子搬了？）⇒ 不信這個錨點、重放");
                    Resummon("mr_anchor_floating");
                }
            }

            _probe.OcclusionKey = !_granted ? null
                : !_occlusionWanted ? "mr_hud_occlusion_off"
                : _depthSeen ? "mr_hud_occlusion_on" : "mr_hud_occlusion_wait";
        }

        private void LateUpdate()
        {
            if (_rig == null) return;
            if (_grabHand >= 0)
            {
                // 拿在手上：跟著捏點走；影子落在她正下方的那一面
                if (_pinch.Holding(_grabHand, out var point)) _rig.Root.transform.position = point + _grabOffset;
                var feet = _rig.Root.transform.position - Vector3.up * _lift;
                if (SurfaceBelow(feet, out var below) != null) _surfaceY = below;
            }
            else if (_anchor != null && _anchor.trackingState == TrackingState.Tracking)
            {
                // 錨點跟著真實房間走（重新定位會微調它）⇒ 她跟著錨點
                _surfaceY = _anchor.transform.position.y;
                _rig.Root.transform.position = _anchor.transform.position + Vector3.up * _lift;
            }
            // M2：她看著你（頭），你繞到太側邊就整個人轉過來
            var eye = _camera.transform.position;
            TurnBody(Bearing(eye), Time.unscaledDeltaTime);
            _rig.LookTarget = eye;
            _probe.Bearing = Bearing(eye);
            Stage3DRig.Light(_camera);
            // M3：等她回覆時「在想」、出聲時嘴照音量開合（手機舞台同一套：NextStage3D）
            var step = Time.unscaledDeltaTime * 4f;
            _think = Mathf.MoveTowards(_think, _bridge.Thinking ? 1f : 0f, step);
            _speak = Mathf.MoveTowards(_speak, _bridge.Speaking ? 1f : 0f, step);
            var level = _bridge.Voice != null && _bridge.Voice.IsPlaying ? (float)_bridge.Voice.LastLevel : -1f;
            _rig.Animate(Time.unscaledTime, Time.unscaledDeltaTime, _yaw, _think, _speak, level);
            // M4：剛召喚 ⇒ 淡入；重新召喚前 ⇒ 淡出（影子跟著一起）
            var fade = _fadeOutAt > 0f
                ? 1f - (Time.unscaledTime - _fadeOutAt) / FadeSeconds
                : (Time.unscaledTime - _spawnedAt) / FadeSeconds;
            fade = Mathf.Clamp01(fade);
            _rig.SetFade(fade);
            if (_shadow != null) _shadow.Follow(_rig, _surfaceY, fade);

            // 頭頂氣泡：在想 ⇒「…」；在說（或這一輪還沒結束）⇒ 她的話；說完留 BubbleLinger 秒再收
            if (_bridge.Busy || _bridge.Speaking) _lastVoiceAt = Time.unscaledTime;
            var said = _bridge.Thinking && string.IsNullOrEmpty(_bridge.Said) ? "…" : _bridge.Said;
            var showing = Time.unscaledTime - _lastVoiceAt < BubbleLinger;
            var standing = _rig.Root.transform.position - Vector3.up * _lift;
            var headTop = standing + Vector3.up * Height;
            // 通話中頭頂一顆聽筒（撥號中一明一暗）；她說話時氣泡疊在聽筒上面
            _callBadge.Show(_call.Active, _call.State == MrCall.Phase.Live, headTop, eye);
            _bubble.Show(showing ? said : null, headTop + Vector3.up * (_call.Active ? MrCallBadge.Height : 0f), eye);
        }

        /// <summary>
        /// 放她（§5.1：桌上 30 cm、固定位置）：**你前方最近的桌面**上離你最近、但至少 <see cref="MinDistance"/>、
        /// 離桌邊 <see cref="EdgeMargin"/> 的地方，面向你，然後把那一點存成錨點。前方沒桌子就找附近任何一張桌子；一張桌子都沒有（Space Setup 沒標 Table）
        /// 就退回站地上、並在儀表明示（不靜默降級）。
        /// 頭顯姿勢還沒進來（相機還在原點）時不放：不然她會出現在「原點前方」而不是你前方。
        /// </summary>
        private void TryPlace()
        {
            var head = _camera.transform;
            if (head.localPosition == Vector3.zero && head.localRotation == Quaternion.identity) return;

            var table = FindTable(head);
            var timedOut = Time.unscaledTime - _waitFrom >= SurfaceTimeout;
            if (table == null && !timedOut) return;

            var flat = Vector3.ProjectOnPlane(head.forward, Vector3.up);
            if (flat.sqrMagnitude < 1e-4f) flat = Vector3.ProjectOnPlane(head.up, Vector3.up);   // 正低頭看
            flat.Normalize();

            float surfaceY;
            Vector3 spot;
            if (table != null)
            {
                _surface = table;
                spot = SpotOnTable(table, head.position, out surfaceY);
                _probe.StatusKey = null;
            }
            else
            {
                _surface = FindFloor(head.position.y);
                spot = head.position + flat * FloorDistance;
                if (_surface != null)
                {
                    surfaceY = _surface.transform.position.y;
                    // 正前方可能是牆後或家具 ⇒ 由遠到近找一個「打得到地面、上面沒東西擋」的點
                    foreach (var distance in new[] { FloorDistance, 0.8f, 0.6f })
                    {
                        var candidate = head.position + flat * distance;
                        if (!OnSurface(candidate, _surface, FloorClearance)) continue;
                        spot = candidate;
                        break;
                    }
                    Warn("mr_no_table");
                }
                else
                {
                    surfaceY = _origin.transform.position.y;    // 追蹤原點＝系統推定的地面
                    Warn(_denied ? "mr_no_permission" : "mr_no_floor");
                }
            }
            spot.y = surfaceY;

            var facing = Vector3.ProjectOnPlane(head.position - spot, Vector3.up);
            if (!Summon(spot, facing, surfaceY, "place")) return;
            // 推定地面上的點沒有意義存起來（下次多半不準）；有平面才存
            if (_surface != null) SaveAnchor(spot, facing);
        }

        /// <summary>把她生在 <paramref name="spot"/>（地面上的一點），腳底對地、面向 <paramref name="facing"/>。</summary>
        private bool Summon(Vector3 spot, Vector3 facing, float floorY, string how)
        {
            try
            {
                _rig = Stage3DRig.Spawn(_bridge.Entity, spot, 0, Height);
            }
            catch (Exception error)
            {
                Debug.LogError($"[mr] 3D 角色載入失敗（{_bridge.Entity}）：{error.Message}");
                Warn("mr_spawn_failed");
                enabled = false;
                return false;
            }
            _rig.SetFade(0f);                                    // 從 passthrough 裡浮現（LateUpdate 淡入）
            _spawnedAt = Time.unscaledTime;
            // 腳底（不是模型原點）對齊地面。⚠️ 只平移：Stage3DMotion 記的是相對胯高，不受影響
            _lift = floorY - _rig.SoleY;
            _rig.Root.transform.position += Vector3.up * _lift;
            _yaw = Vector3.SignedAngle(_rig.Forward, facing, Vector3.up);
            _surfaceY = floorY;

            _probe.Track(_rig, _surface, floorY);
            Debug.Log($"[mr] 召喚 {_bridge.Entity}（{how}）：位置 {spot}（所站面 y={floorY:F3}，來源="
                      + (how == "restore" ? "錨點"
                         : _surface != null ? $"平面 {_surface.trackableId}／{_surface.classifications}" : "推定（無平面）")
                      + $"）身高 {_rig.Bounds.size.y:F2} m、腳底修正 {_lift * 100f:F1} cm、轉 {_yaw:F0}°");
            return true;
        }

        /// <summary>
        /// 上次存的錨點 → 載回來 → 等它重新定位 → 她站回那一點。任何一步不成就忘掉它，下一幀照 M0 重放（並重新存）。
        /// </summary>
        private async void LoadSaved()
        {
            _busy = true;
            _triedLoad = true;
            _probe.StatusKey = "mr_hud_restoring";
            try
            {
                var descriptor = _anchors.descriptor;
                if (!Guid.TryParse(PlayerPrefs.GetString(KeyAnchor), out var id)
                    || descriptor == null || !descriptor.supportsLoadAnchor)
                {
                    Debug.LogWarning("[mr] 錨點：存的 GUID 讀不了，或這台不支援載入 ⇒ 重放");
                    Forget();
                    return;
                }
                var result = await _anchors.TryLoadAnchorAsync(new SerializableGuid(id));
                if (!result.status.IsSuccess() || result.value == null)
                {
                    Debug.LogWarning($"[mr] 錨點 {id} 載入失敗（{result.status}）⇒ 重放");
                    Forget();
                    return;
                }
                var anchor = result.value;
                var until = Time.unscaledTime + RelocalizeTimeout;
                while (anchor != null && anchor.trackingState != TrackingState.Tracking && Time.unscaledTime < until)
                    await Awaitable.NextFrameAsync();
                if (anchor == null || anchor.trackingState != TrackingState.Tracking)
                {
                    Debug.LogWarning($"[mr] 錨點 {id} 載入了但 {RelocalizeTimeout:F0} s 內沒重新定位（換了房間？）⇒ 重放");
                    if (anchor != null) _anchors.TryRemoveAnchor(anchor);
                    Forget();
                    return;
                }

                _anchor = anchor;
                var at = anchor.transform;
                _surface = SurfaceUnder(at.position);
                if (!Summon(at.position, at.forward, at.position.y, "restore")) return;
                _probe.StatusKey = null;
                _probe.Note = NextStrings.Get("mr_anchor_restored_noref");
                _measureRestore = PlayerPrefs.HasKey(KeyAnchorSurface);
                _restoredAt = Time.unscaledTime;
                Debug.Log($"[mr] 錨點 {id}：站回上次的位置");
            }
            catch (Exception error)
            {
                Debug.LogError("[mr] 錨點載入出錯：" + error);
                Forget();
            }
            finally
            {
                _busy = false;
            }
        }

        /// <summary>在她腳下那一點加錨點並存成持久的；GUID 與「她在地面平面座標系的位置」記進 PlayerPrefs。</summary>
        private async void SaveAnchor(Vector3 spot, Vector3 facing)
        {
            _busy = true;
            try
            {
                var added = await _anchors.TryAddAnchorAsync(new Pose(spot, Quaternion.LookRotation(facing, Vector3.up)));
                if (!added.status.IsSuccess() || added.value == null)
                {
                    Debug.LogWarning($"[mr] 錨點：加不上（{added.status}）");
                    Notify(NextStrings.Get("mr_anchor_failed"));
                    return;
                }
                _anchor = added.value;
                var descriptor = _anchors.descriptor;
                if (descriptor == null || !descriptor.supportsSaveAnchor)
                {
                    Debug.LogWarning("[mr] 錨點：這台不支援存（只在這一次有效）");
                    Notify(NextStrings.Get("mr_anchor_failed"));
                    return;
                }
                var saved = await _anchors.TrySaveAnchorAsync(_anchor);
                if (!saved.status.IsSuccess())
                {
                    Debug.LogWarning($"[mr] 錨點：存不了（{saved.status}）");
                    Notify(NextStrings.Get("mr_anchor_failed"));
                    return;
                }
                var guid = saved.value.guid.ToString();
                PlayerPrefs.SetString(KeyAnchor, guid);
                if (_surface != null)
                {
                    var local = _surface.transform.InverseTransformPoint(spot);
                    PlayerPrefs.SetString(KeyAnchorSurface, string.Join("|", _surface.trackableId.ToString(),
                        Num(local.x), Num(local.y), Num(local.z)));
                }
                else
                {
                    PlayerPrefs.DeleteKey(KeyAnchorSurface);
                }
                PlayerPrefs.Save();
                // 「記住這個位置」只是量測（儀表看）；重放有原因（桌子搬了…）才要跟你說
                if (string.IsNullOrEmpty(_placeWhy)) _probe.Note = NextStrings.Get("mr_anchor_saved");
                else Notify(NextStrings.Get(_placeWhy));
                _placeWhy = null;
                Debug.Log($"[mr] 錨點：存好了 {guid}");
            }
            catch (Exception error)
            {
                Debug.LogError("[mr] 錨點存檔出錯：" + error);
                Notify(NextStrings.Get("mr_anchor_failed"));
            }
            finally
            {
                _busy = false;
            }
        }

        /// <summary>
        /// M1 驗收 ①（重開回原位差多少）：存的時候記下她在**地面平面**座標系的位置；Space Setup 的平面是跨次啟動穩定的參照
        /// ⇒ 重開後把那個位置換回世界、跟錨點比距離。平面 id 對不上（重做過 Space Setup）就量不了，照實說。
        /// </summary>
        private void MeasureRestore()
        {
            _measureRestore = false;
            var parts = PlayerPrefs.GetString(KeyAnchorSurface).Split('|');
            if (_anchor == null || parts.Length != 4 || parts[0] != _surface.trackableId.ToString()
                || !float.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var x)
                || !float.TryParse(parts[2], NumberStyles.Float, CultureInfo.InvariantCulture, out var y)
                || !float.TryParse(parts[3], NumberStyles.Float, CultureInfo.InvariantCulture, out var z))
            {
                Debug.Log($"[mr] 錨點還原誤差：量不了（存的平面 {(parts.Length > 0 ? parts[0] : "-")}、現在 {_surface.trackableId}）");
                return;
            }
            var expected = _surface.transform.TransformPoint(new Vector3(x, y, z));
            var error = Vector3.Distance(expected, _anchor.transform.position);
            _probe.Note = NextStrings.F("mr_anchor_restored", "err", (error * 100f).ToString("F1", CultureInfo.InvariantCulture));
            Debug.Log($"[mr] 錨點還原誤差 restoreErr={error * 100f:F1}cm（相對所站平面 {_surface.trackableId}）");
        }

        /// <summary>手柄 B／Y：清掉存的錨點、收掉她，下一幀照 M0 放在你現在的正前方（並重新存）。</summary>
        /// <param name="why">重放的原因（i18n key；放好後顯示在儀表上）。手勢、手柄重放不帶。</param>
        private async void Resummon(string why = null)
        {
            if (_busy || _switching || _rig == null) return;
            _busy = true;
            try
            {
                // 先淡出（她不是瞬間消失，是在原地慢慢透掉）
                _fadeOutAt = Time.unscaledTime;
                while (Time.unscaledTime - _fadeOutAt < FadeSeconds) await Awaitable.NextFrameAsync();
                var descriptor = _anchors.descriptor;
                if (Guid.TryParse(PlayerPrefs.GetString(KeyAnchor), out var id)
                    && descriptor != null && descriptor.supportsEraseAnchor)
                {
                    var erased = await _anchors.TryEraseAnchorAsync(new SerializableGuid(id));
                    Debug.Log($"[mr] 錨點 {id}：清掉（{erased}）");
                }
                Forget();
                if (_anchor != null) _anchors.TryRemoveAnchor(_anchor);
                _anchor = null;
                _rig?.Dispose();
                _rig = null;
                _measureRestore = false;
                _triedLoad = true;
                _probe.Track(null, null, 0f);
                _probe.Note = null;
                _placeWhy = why;                                 // 放好、存好錨點時才顯示（SaveAnchor）
                _probe.StatusKey = "mr_hud_waiting";
                if (_shadow != null) _shadow.Follow(null, 0f);
                Debug.Log("[mr] 重新召喚");
            }
            catch (Exception error)
            {
                Debug.LogError("[mr] 重新召喚出錯：" + error);
            }
            finally
            {
                _fadeOutAt = -1f;
                _busy = false;
            }
        }

        /// <summary>
        /// 你在她身體正前方的哪一邊（度；正＝她的左手邊）。離她不到 20 cm（水平）＝幾乎在她正上方，當正前方
        /// （跟頭的 `LookOverhead` 同一個門檻：那時左右沒有意義，也不該轉身）。
        /// </summary>
        private float Bearing(Vector3 eye)
        {
            var facing = Vector3.ProjectOnPlane(Quaternion.AngleAxis(_yaw, Vector3.up) * _rig.Forward, Vector3.up);
            var to = Vector3.ProjectOnPlane(eye - _rig.Root.transform.position, Vector3.up);
            return to.sqrMagnitude < 0.2f * 0.2f ? 0f : Vector3.SignedAngle(facing, to, Vector3.up);
        }

        /// <summary>你超出 <see cref="BodyTurnStart"/> 就開始轉身，轉到剩 <see cref="BodyTurnStop"/> 以內停（中間不來回抖）。</summary>
        private void TurnBody(float bearing, float dt)
        {
            if (!_turning && Mathf.Abs(bearing) > BodyTurnStart) _turning = true;
            if (!_turning) return;
            var step = Mathf.Clamp(bearing, -BodyTurnSpeed * dt, BodyTurnSpeed * dt);
            _yaw += step;
            if (Mathf.Abs(bearing - step) < BodyTurnStop) _turning = false;
        }

        // ── M3：捏手指 ─────────────────────────────────────────────────────────────

        /// <summary>
        /// 捏下去（WO §4 的順序）：捏在她身上 ⇒ 拿起來；同一隻手上一下放開後 <see cref="DoublePinch"/> 秒內 ⇒ 雙擊
        /// （右手＝通話開關、左手＝換人）；不然開始計時（放開時沒到 <see cref="LongPinch"/> ＝ 一下；一直捏著到了 ＝ 重新召喚）。
        /// </summary>
        private void OnPinchDown(int hand, Vector3 point)
        {
            if (_grabHand >= 0) return;                          // 一隻手拿著她時，另一隻手不算
            if (_rig != null && !_busy && !_switching && OnHer(point))
            {
                BeginGrab(hand, point);
                return;
            }
            if (_lastTapAt > 0f && hand == _lastTapHand && Time.unscaledTime - _lastTapAt <= DoublePinch)
            {
                Debug.Log($"[mr] 捏：{(hand == 0 ? "左" : "右")}手第二下（放開後 {Time.unscaledTime - _lastTapAt:F2} s）"
                          + (hand == 1 ? "⇒ 通話開關" : "⇒ 換人"));
                _lastTapAt = -1f;
                _holdHand = -1;
                if (hand == 1) ToggleCall();
                else SwitchCharacter();
            }
            else
            {
                // 第一下：放開時算它是「一下」還是長按（Update 裡一直捏著到 LongPinch 就召回）
                _lastTapAt = -1f;
                _holdHand = hand;
                _holdStart = Time.unscaledTime;
            }
        }

        private void OnPinchUp(int hand, Vector3 point)
        {
            if (hand == _grabHand) EndGrab();
            if (hand != _holdHand) return;
            _holdHand = -1;
            // 短於長按 ⇒ 算一下，從「放開」起等同一隻手的第二下；捏多久記下來（之後調門檻用）
            var held = Time.unscaledTime - _holdStart;
            _lastTapAt = Time.unscaledTime;
            _lastTapHand = hand;
            Debug.Log($"[mr] 捏：{(hand == 0 ? "左" : "右")}手一下（捏了 {held:F2} s）");
        }

        /// <summary>右手雙擊：通話開／關。提示音：開＝往上、關＝往下（撥不出去的原因寫在儀表上）。</summary>
        private void ToggleCall()
        {
            var opening = !_call.Active;
            _call.Toggle();
            _cue.PlayOneShot(opening && _call.Active ? _cueStart : _cueStop);
        }

        /// <summary>
        /// 左手雙擊：在**當前落點**換下一位（WO §4：位置／朝向／所站面／錨點全部不動，只換模型；錨點不重存）。
        /// 通話中 ⇒ 先掛（一通只對一個角色），儀表明示。淡出 → 換模型 → 淡入，約 0.8 秒。
        /// </summary>
        private async void SwitchCharacter()
        {
            if (_rig == null || _busy || _switching) return;
            var next = _bridge.NextEntity();
            if (string.IsNullOrEmpty(next))
            {
                Notify(NextStrings.Get("mr_switch_none"));
                Debug.Log("[mr] 換人：沒有下一位（還沒連上，或只有她一個有 3D 模型）");
                return;
            }
            _switching = true;
            try
            {
                if (_call.Active)
                {
                    _call.Hangup(NextStrings.Get("mr_switch_hangup"));
                    Debug.Log("[mr] 換人：通話中 ⇒ 先掛");
                }
                _cue.PlayOneShot(_cueStart);
                _fadeOutAt = Time.unscaledTime;
                while (Time.unscaledTime - _fadeOutAt < FadeSeconds) await Awaitable.NextFrameAsync();
                if (_rig == null) return;                         // 淡出途中被收掉了（不會發生，保險）
                var feet = _rig.Root.transform.position - Vector3.up * _lift;
                var from = _bridge.Entity;
                _rig.Dispose();
                _rig = null;
                _bridge.SetEntity(next);
                try
                {
                    _rig = Stage3DRig.Spawn(next, new Vector3(feet.x, _surfaceY, feet.z), 0, Height);
                }
                catch (Exception error)
                {
                    // 換不過去 ⇒ 把原來那位放回去，不要讓桌上空著
                    Debug.LogError($"[mr] 換人：{next} 載入失敗（{error.Message}）⇒ 換回 {from}");
                    _bridge.SetEntity(from);
                    _rig = Stage3DRig.Spawn(from, new Vector3(feet.x, _surfaceY, feet.z), 0, Height);
                    Notify(NextStrings.Get("mr_spawn_failed"));
                }
                // 腳底對齊所站面（跟 Summon 同一個算法）；_yaw 不動 ⇒ 朝向一點不變
                _lift = _surfaceY - _rig.SoleY;
                _rig.Root.transform.position += Vector3.up * _lift;
                _rig.SetFade(0f);
                _spawnedAt = Time.unscaledTime;
                _probe.Track(_rig, _surface, _surfaceY);
                PlayerPrefs.SetString(KeyCharacter, _bridge.Entity);
                PlayerPrefs.Save();
                if (_rig != null && _bridge.Entity == next) Notify(NextStrings.F("mr_switched", "name", _bridge.Name));
                Debug.Log($"[mr] 換人：{from} ⇒ {_bridge.Entity}（{_bridge.Name}）位置 {feet} 不動、轉 {_yaw:F0}° 不動、錨點不重存");
            }
            catch (Exception error)
            {
                Debug.LogError("[mr] 換人出錯：" + error);
            }
            finally
            {
                _fadeOutAt = -1f;
                _switching = false;
            }
        }

        /// <summary>捏點在不在她身邊的抓取膠囊裡（腳 → 頭頂那條線段，半徑 <see cref="GrabRadius"/>）。</summary>
        private bool OnHer(Vector3 point)
        {
            var feet = _rig.Root.transform.position - Vector3.up * _lift;
            var top = feet + Vector3.up * Height;
            var along = Mathf.Clamp01(Vector3.Dot(point - feet, top - feet) / (Height * Height));
            return Vector3.Distance(point, feet + (top - feet) * along) <= GrabRadius;
        }

        private void BeginGrab(int hand, Vector3 point)
        {
            _grabHand = hand;
            _grabOffset = _rig.Root.transform.position - point;
            _grabFrom = _rig.Root.transform.position;
            _lastTapAt = -1f;
            _cue.PlayOneShot(_cueStart);
            Debug.Log($"[mr] 拿起來（{(hand == 0 ? "左" : "右")}手）");
        }

        /// <summary>
        /// 放手：她正下方 <see cref="DropSearch"/> 以內有平面（桌面、地面）⇒ 落在上面、錨點改存這裡；
        /// 下面什麼都沒有（手伸到桌子外面）⇒ 回原位。
        /// </summary>
        private void EndGrab()
        {
            _grabHand = -1;
            var feet = _rig.Root.transform.position - Vector3.up * _lift;
            var surface = SurfaceBelow(feet, out var y);
            if (surface == null)
            {
                _rig.Root.transform.position = _grabFrom;
                Debug.Log("[mr] 放手：下面沒有平面 ⇒ 回原位");
                return;
            }
            var spot = new Vector3(feet.x, y, feet.z);
            _rig.Root.transform.position = spot + Vector3.up * _lift;
            _surface = surface;
            _surfaceY = y;
            _probe.Track(_rig, _surface, y);
            _cue.PlayOneShot(_cueStop);
            Debug.Log($"[mr] 放下：{spot}（{surface.classifications}）");
            var facing = Vector3.ProjectOnPlane(Quaternion.AngleAxis(_yaw, Vector3.up) * _rig.Forward, Vector3.up);
            MoveAnchor(spot, facing);
        }

        /// <summary>她正下方（從腳往上 5 cm 開始往下打）最近的那一片平面與它的高度。</summary>
        private ARPlane SurfaceBelow(Vector3 feet, out float y)
        {
            y = 0f;
            var ray = new Ray(feet + Vector3.up * 0.05f, Vector3.down);
            if (!_raycasts.Raycast(ray, _hits, TrackableType.PlaneWithinPolygon)) return null;
            foreach (var hit in _hits)
            {
                if (feet.y + 0.05f - hit.pose.position.y > DropSearch) continue;
                var plane = _planes.GetPlane(hit.trackableId);
                if (plane == null || plane.alignment != PlaneAlignment.HorizontalUp) continue;
                y = hit.pose.position.y;
                return plane;                                    // _hits 依距離排序 ⇒ 第一個就是最近的
            }
            return null;
        }

        /// <summary>放到新位置：清掉舊錨點、在新的一點存一個（錨點操作進行中就只動位置、log 一聲）。</summary>
        private async void MoveAnchor(Vector3 spot, Vector3 facing)
        {
            if (_busy)
            {
                Debug.LogWarning("[mr] 放下：錨點正在存／清 ⇒ 這次只動位置、沒存");
                return;
            }
            _busy = true;
            try
            {
                var descriptor = _anchors.descriptor;
                if (Guid.TryParse(PlayerPrefs.GetString(KeyAnchor), out var id)
                    && descriptor != null && descriptor.supportsEraseAnchor)
                    await _anchors.TryEraseAnchorAsync(new SerializableGuid(id));
                Forget();
                if (_anchor != null) _anchors.TryRemoveAnchor(_anchor);
                _anchor = null;
            }
            catch (Exception error)
            {
                Debug.LogError("[mr] 放下時清錨點出錯：" + error);
            }
            finally
            {
                _busy = false;
            }
            SaveAnchor(spot, facing);
        }

        /// <summary>儀表上的對話那幾行：橋的狀態、正在聽、你說了什麼（她說的在頭頂氣泡）。</summary>
        private string Conversation()
        {
            var lines = new List<string>();
            if (_bridge.Ready) lines.Add(NextStrings.F("mr_talking_to", "name", _bridge.Name));
            if (!string.IsNullOrEmpty(_bridge.Status)) lines.Add(_bridge.Status);
            if (_call.MicDenied) lines.Add(NextStrings.Get("mr_mic_denied"));
            if (_bridge.Ready && !_pinch.Running) lines.Add(NextStrings.Get("mr_hands_off"));
            var call = _call.Line;
            if (!string.IsNullOrEmpty(call)) lines.Add(call);
            if (!string.IsNullOrEmpty(_bridge.Heard)) lines.Add(NextStrings.F("mr_heard", "text", _bridge.Heard));
            // 她的話在她頭頂的氣泡裡（MrBubble），儀表只留你的那一句
            return string.Join("\n", lines);
        }

        /// <summary>提示音：幾個音高接起來的短嗶（開始＝往上、結束＝往下），淡入淡出不爆音。</summary>
        private static AudioClip Beep(string name, float[] tones)
        {
            const int rate = 44100;
            const float each = 0.07f;
            var per = (int)(rate * each);
            var data = new float[per * tones.Length];
            for (var t = 0; t < tones.Length; t++)
            {
                for (var i = 0; i < per; i++)
                {
                    var envelope = Mathf.Sin(Mathf.PI * i / per);
                    data[t * per + i] = Mathf.Sin(2f * Mathf.PI * tones[t] * i / rate) * 0.25f * envelope;
                }
            }
            var clip = AudioClip.Create(name, data.Length, 1, rate, false);
            clip.SetData(data, 0);
            return clip;
        }

        private static void Forget()
        {
            PlayerPrefs.DeleteKey(KeyAnchor);
            PlayerPrefs.DeleteKey(KeyAnchorSurface);
            PlayerPrefs.Save();
        }

        /// <summary>
        /// passthrough 是 Unity OpenXR Meta 在場景根上建的一個 `Passthrough` 物件（合成層）。沒建起來 ⇒ 她照畫、背景全黑，
        /// 而且套件不報錯（2026-10-31 實機：少開 OpenXR 的 Composition Layers 功能就是這樣）⇒ 自己查、畫在眼前。
        /// </summary>
        private void CheckPassthrough()
        {
            if (Time.unscaledTime < _passthroughNextCheck) return;
            _passthroughNextCheck = Time.unscaledTime + 1f;
            var found = GameObject.Find("Passthrough") != null;
            if (!found && Time.unscaledTime < PassthroughGiveUpAt) return;   // 還在等，下一秒再看
            _passthroughChecked = true;
            Debug.Log($"[mr] passthrough 合成層：{(found ? "有" : "沒有（背景會是黑的）")}（{Time.unscaledTime:F1} s）");
            if (!found) Notify(NextStrings.Get("mr_no_passthrough"));
        }

        /// <summary>手柄 A／X：遮擋開關（M1 驗收 ③：開與不開的幀率都要記）。</summary>
        private void ToggleOcclusion()
        {
            _occlusionWanted = !_occlusionWanted;
            if (_granted) _occlusion.enabled = _occlusionWanted;
            _shaderOcclusion.enabled = _occlusionWanted && _depthSeen;
            Debug.Log($"[mr] 遮擋：{(_occlusionWanted ? "開" : "關")}");
        }

        private void OnDepthFrame(AROcclusionFrameEventArgs args)
        {
            if (_depthSeen) return;
            _depthSeen = true;
            _shaderOcclusion.enabled = _occlusionWanted;
            Debug.Log($"[mr] 環境深度：第一幀到了 ⇒ 開遮擋（{_shaderOcclusion.occlusionShaderMode}）");
        }

        /// <summary>
        /// 地面平面：Space Setup 標成 Floor 的那一片（有多片取最大）；沒有分類就取「比頭低 1 m 以上」最低的朝上平面。
        /// </summary>
        private ARPlane FindFloor(float headY)
        {
            ARPlane floor = null;
            ARPlane lowest = null;
            foreach (var plane in _planes.trackables)
            {
                if (plane.alignment != PlaneAlignment.HorizontalUp) continue;
                if ((plane.classifications & PlaneClassifications.Floor) != 0)
                {
                    if (floor == null || Area(plane) > Area(floor)) floor = plane;
                }
                else if (plane.transform.position.y < headY - 1f
                         && (lowest == null || plane.transform.position.y < lowest.transform.position.y))
                {
                    lowest = plane;
                }
            }
            return floor != null ? floor : lowest;
        }

        private static float Area(ARPlane plane) => plane.size.x * plane.size.y;

        private static string Num(float value) => value.ToString("R", CultureInfo.InvariantCulture);

        /// <summary>
        /// 你前方 <see cref="MaxTableDistance"/> 以內最近的桌面（Space Setup 標成 Table 的朝上平面）；
        /// 前方（左右約 ±70°）沒有就取附近任何一張。
        /// </summary>
        private ARPlane FindTable(Transform head)
        {
            var forward = Vector3.ProjectOnPlane(head.forward, Vector3.up).normalized;
            ARPlane front = null, any = null;
            float frontDistance = float.MaxValue, anyDistance = float.MaxValue;
            foreach (var plane in _planes.trackables)
            {
                if (plane.alignment != PlaneAlignment.HorizontalUp) continue;
                if ((plane.classifications & PlaneClassifications.Table) == 0) continue;
                var to = Vector3.ProjectOnPlane(plane.center - head.position, Vector3.up);
                var distance = to.magnitude;
                if (distance > MaxTableDistance) continue;
                if (distance < anyDistance) { any = plane; anyDistance = distance; }
                var inFront = distance < 0.3f || Vector3.Dot(to / distance, forward) > 0.35f;
                if (inFront && distance < frontDistance) { front = plane; frontDistance = distance; }
            }
            return front != null ? front : any;
        }

        /// <summary>
        /// 桌面上她站哪：從桌子中心往你這邊每 5 cm 挪一步，挪到「再往你這邊 <see cref="EdgeMargin"/> 就出桌面」為止
        /// ——靠近你、但不站在邊緣上。每一步都要打得到這張桌子、上方沒有別的平面（層板）擋著。
        /// </summary>
        private Vector3 SpotOnTable(ARPlane table, Vector3 headPosition, out float surfaceY)
        {
            var center = table.center;
            surfaceY = center.y;
            var toUser = Vector3.ProjectOnPlane(headPosition - center, Vector3.up);
            var reach = toUser.magnitude;
            if (reach < 1e-3f) return center;
            var direction = toUser / reach;
            // 沿著「你 → 桌心」這條線，從靠你那一端往遠處每 5 cm 試：第一個「離你 ≥ MinDistance、兩邊離桌邊 ≥ EdgeMargin、
            // 上方沒東西擋」的點就是她站的地方（靠近你、但不貼臉也不站桌邊）。全都不行就站桌心。
            for (var offset = reach; offset > -reach - 1f; offset -= 0.05f)
            {
                var candidate = center + direction * offset;
                var flat = Vector3.ProjectOnPlane(headPosition - candidate, Vector3.up);
                if (flat.magnitude < MinDistance) continue;
                if (!OnSurface(candidate, table, TableClearance)
                    || !OnSurface(candidate + direction * EdgeMargin, table, TableClearance)
                    || !OnSurface(candidate - direction * EdgeMargin, table, TableClearance)) continue;
                return candidate;
            }
            return center;
        }

        /// <summary>
        /// 從 <paramref name="point"/> 正上方 <paramref name="clearance"/> 往下打平面：打得到 <paramref name="surface"/>、
        /// 而且中間沒有別的平面擋著（地面上的桌子、桌面上的層板）才算。
        /// </summary>
        private bool OnSurface(Vector3 point, ARPlane surface, float clearance)
        {
            var y = surface.transform.position.y;
            var ray = new Ray(new Vector3(point.x, y + clearance, point.z), Vector3.down);
            if (!_raycasts.Raycast(ray, _hits, TrackableType.PlaneWithinPolygon)) return false;
            var on = false;
            foreach (var hit in _hits)
            {
                if (hit.pose.position.y > y + 0.05f) return false;   // 她會站進家具裡
                if (hit.trackableId == surface.trackableId) on = true;
            }
            return on;
        }

        /// <summary>這一點正下方（3 cm 內）是哪一片平面：從錨點載回來時用，接上儀表與還原誤差的參照。</summary>
        private ARPlane SurfaceUnder(Vector3 point)
        {
            var ray = new Ray(point + Vector3.up * 0.3f, Vector3.down);
            if (!_raycasts.Raycast(ray, _hits, TrackableType.PlaneWithinPolygon)) return null;
            foreach (var hit in _hits)
            {
                if (Mathf.Abs(hit.pose.position.y - point.y) <= 0.03f) return _planes.GetPlane(hit.trackableId);
            }
            return null;
        }

        /// <summary>你該知道的事：除錯儀表那一行照寫，同時在視野下方跳一則通知。</summary>
        private void Notify(string text)
        {
            _probe.Note = text;
            _toast.Post(text);
        }

        /// <summary>放不好她的那幾種狀況（沒桌子、沒權限、模型載不到）：儀表第一行＋通知。</summary>
        private void Warn(string key)
        {
            _probe.StatusKey = key;
            _toast.Post(NextStrings.Get(key));
        }

        private void OnDestroy()
        {
            _toggleOcclusion?.Dispose();
            _resummon?.Dispose();
            _toggleProbe?.Dispose();
            if (_occlusion != null) _occlusion.frameReceived -= OnDepthFrame;
            _rig?.Dispose();
            _rig = null;
        }
    }
}
