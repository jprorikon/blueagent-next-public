using System;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Logic.Motion;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 立繪舞台的 **3D 模式**（owner 2026-10-30：「先用現在的模型……在現在顯示立繪的位置切換顯示 3D 模型」）。
    ///
    /// 做法跟舊殼的模型層同一條思路：模型擺在世界裡一個看不到的角落、自己一台相機，
    /// **畫進 RenderTexture**，UI 用一顆 `RawImage` 貼在舞台那一格 ⇒ 剪裁免費、不靠深度跟畫布打架、
    /// 底色就是舞台自己的背景（相機 clear 成透明）。
    ///
    /// 資產（`Resources/models3d/&lt;角色 id&gt;/`，由 `tools/sync_stage3d_models.py` 產生）：
    ///   `model.fbx`＋貼圖＋`stage3d.json`（留哪些節點、哪些掛到頭上、材質 → 貼圖）。
    /// 這個專案**不進版控 `.meta`** ⇒ 不能在編輯器裡把材質接好存成資產（GUID 會斷），
    /// 一律**執行期照名字組裝**：卡通著色 `BlueAgent/Stage3DToon`（建置時釘進 Always Included，見 `HeadlessBuild`）＋貼圖；
    /// 嘴換成共用的嘴型圖集（`Character_Mouth`），表情跟著 normal／thinking／speaking 換格。
    ///
    /// 模型是 外部模型網格：有骨架與權重，**沒有動畫、沒有 blendshape** ⇒ 這裡只做程式化的待機
    /// （手臂從 T-pose 放下、呼吸、身體微晃、頭部轉動；thinking 歪頭、speaking 點頭），可以拖曳轉身。
    /// 真的對嘴型（照 TTS 的嘴型時間軸換格）與表情要等 PLAN-3d-vr Step 2。
    /// </summary>
    public sealed class NextStage3D : MonoBehaviour, IBeginDragHandler, IDragHandler, IEndDragHandler
    {
        /// <summary>模型與模型相機專用的 layer（主相機不看它）。</summary>
        public const int Layer = 31;

        /// <summary>模型擺在世界的哪裡（遠離 UI 畫布，主相機的視野碰不到）。</summary>
        private static readonly Vector3 Origin = new Vector3(0f, -1000f, 0f);

        private const int MaxSide = 1024;
        /// <summary>放手後多久開始轉回正面（秒）。</summary>
        private const float ReturnDelay = 2.5f;

        /// <summary>現在畫面上那一個（`NextShell.SetStage` 把說話狀態轉給它）。</summary>
        public static NextStage3D Current { get; private set; }

        /// <summary>
        /// 她的語音現在多大聲（RMS，0–1；沒在播 ⇒ 負數）。`NextShell` 接上 `SpeechPlayer.LastLevel`。
        /// 有值時嘴照音量開合；沒有（guest 沒聲音、文字回覆）就退回固定節奏換嘴型。
        /// </summary>
        public static Func<float> SpeechLevel;

        private static readonly Dictionary<string, bool> Known = new Dictionary<string, bool>();

        /// <summary>這個角色有沒有 3D 模型（看 `Resources/models3d/&lt;id&gt;/stage3d.json`；結果快取）。</summary>
        public static bool Has(string character)
        {
            if (string.IsNullOrEmpty(character)) return false;
            if (Known.TryGetValue(character, out var known)) return known;
            var yes = Resources.Load<TextAsset>(Stage3DRig.Folder(character) + "stage3d") != null;
            Known[character] = yes;
            return yes;
        }

        /// <summary>載入失敗（模型／shader 找不到）時叫：舞台退回 2D。</summary>
        public event Action Failed;

        private RawImage _slot;
        private string _character;
        private string _state = AvatarTimeline.Normal;
        private Stage3DRig _rig;
        private Camera _camera;
        private RenderTexture _rt;
        private float _yaw;
        /// <summary>舞台那一格在螢幕上的寬（畫素；拖曳的 delta 是畫素，要用同一個單位換角度）。</summary>
        private float _screenWidth = 1f;
        private bool _dragging;
        private float _releasedAt;
        private float _thinking;
        private float _speaking;

        /// <summary>在舞台那一格上面鋪一層 3D（不參與排版、撐滿）。</summary>
        public static NextStage3D Create(RectTransform holder, string character, string state)
        {
            var rect = UiBuilder.Overlay("Stage3D", holder);
            var slot = rect.gameObject.AddComponent<RawImage>();
            slot.raycastTarget = true;           // 拖曳轉身
            slot.enabled = false;                // RT 畫出第一張之前不畫（免得閃一格空白）
            var view = rect.gameObject.AddComponent<NextStage3D>();
            view._slot = slot;
            view._character = character;
            view._state = string.IsNullOrEmpty(state) ? AvatarTimeline.Normal : state;
            return view;
        }

        private MotionClip _pendingMotion;

        /// <summary>播一段動作；模型還沒載好就等載好再播。</summary>
        public void PlayMotion(MotionClip clip)
        {
            if (clip == null) return;
            if (_rig == null) { _pendingMotion = clip; return; }
            _rig.PlayMotion(clip);
            Debug.Log($"[next] 3D 動作：播 {clip.Tag}（{clip.Id}，{clip.Seconds:F1} s）");
        }

        public void SetState(string state)
        {
            _state = string.IsNullOrEmpty(state) ? AvatarTimeline.Normal : state;
        }

        private void Start()
        {
            try
            {
                _rig = Stage3DRig.Spawn(_character, Origin, Layer);
            }
            catch (Exception error)
            {
                Debug.LogWarning($"[next] 3D 立繪載入失敗（{_character}）：{error.Message}");
                Failed?.Invoke();
                Destroy(gameObject);
                return;
            }

            _camera = Stage3DRig.NewCamera("Stage3DCamera", Layer);
            _camera.depth = -20f;                                    // 比畫 UI 的主相機早 ⇒ 同一幀就貼得到

            // 其他相機（畫 UI 的主相機）一律不看模型層
            foreach (var other in Camera.allCameras)
            {
                if (other != _camera) other.cullingMask &= ~(1 << Layer);
            }

            Current = this;
            Debug.Log($"[next] 3D 立繪：{_character}（bounds={_rig.Bounds.size}）");
            if (_pendingMotion != null) { _rig.PlayMotion(_pendingMotion); _pendingMotion = null; }
        }

        private void LateUpdate()
        {
            if (_rig == null || _camera == null) return;

            var size = TargetSize();
            if (_rt == null || _rt.width != size.x || _rt.height != size.y)
            {
                if (_rt != null)
                {
                    _camera.targetTexture = null;
                    _rt.Release();
                    Destroy(_rt);
                }
                _rt = new RenderTexture(size.x, size.y, 24, RenderTextureFormat.ARGB32)
                {
                    name = $"Stage3DRT {size.x}x{size.y}",
                    antiAliasing = 4,
                };
                _rt.Create();
                _camera.targetTexture = _rt;
                _slot.texture = _rt;
                _rig.Aim(_camera, (float)size.x / size.y);
            }
            Stage3DRig.Light(_camera);
            _slot.enabled = true;

            // 放手一陣子之後慢慢轉回正面
            if (!_dragging && Time.unscaledTime - _releasedAt > ReturnDelay)
                _yaw = Mathf.LerpAngle(_yaw, 0f, 1f - Mathf.Exp(-2f * Time.unscaledDeltaTime));

            var step = Time.unscaledDeltaTime * 4f;
            _thinking = Mathf.MoveTowards(_thinking, _state == AvatarTimeline.Thinking ? 1f : 0f, step);
            _speaking = Mathf.MoveTowards(_speaking, _state == AvatarTimeline.Speaking ? 1f : 0f, step);
            var level = SpeechLevel != null ? SpeechLevel() : -1f;
            _rig.Animate(Time.unscaledTime, Time.unscaledDeltaTime, _yaw, _thinking, _speaking, level);
        }

        /// <summary>格子在螢幕上的畫素大小，等比縮到 <see cref="MaxSide"/> 以內。</summary>
        private Vector2Int TargetSize()
        {
            var rect = _slot.rectTransform;
            var w = rect.rect.width * 2f;
            var h = rect.rect.height * 2f;
            var canvas = _slot.canvas != null ? _slot.canvas.rootCanvas : null;
            if (canvas != null)
            {
                var corners = new Vector3[4];
                rect.GetWorldCorners(corners);
                Camera eye = null;
                if (canvas.renderMode != RenderMode.ScreenSpaceOverlay)
                    eye = canvas.worldCamera != null ? canvas.worldCamera : Camera.main;
                var a = RectTransformUtility.WorldToScreenPoint(eye, corners[0]);
                var b = RectTransformUtility.WorldToScreenPoint(eye, corners[2]);
                if (Mathf.Abs(b.x - a.x) > 1f && Mathf.Abs(b.y - a.y) > 1f)
                {
                    w = Mathf.Abs(b.x - a.x);
                    h = Mathf.Abs(b.y - a.y);
                }
            }
            _screenWidth = Mathf.Max(1f, w);
            var scale = Mathf.Min(1f, MaxSide / Mathf.Max(1f, Mathf.Max(w, h)));
            return new Vector2Int(Mathf.Max(64, Mathf.RoundToInt(w * scale)),
                                  Mathf.Max(64, Mathf.RoundToInt(h * scale)));
        }

        public void OnBeginDrag(PointerEventData eventData) => _dragging = true;

        public void OnDrag(PointerEventData eventData)
        {
            // 拖過整格寬＝轉 240°。⚠️ delta 是**螢幕畫素**：除以畫布單位的寬，Retina（3×）上會快三倍
            // （2026-10-30 iOS 模擬器實測：拖 120 pt 轉了快 180°）。
            _yaw -= eventData.delta.x / _screenWidth * 240f;
        }

        public void OnEndDrag(PointerEventData eventData)
        {
            _dragging = false;
            _releasedAt = Time.unscaledTime;
        }

        private void OnDestroy()
        {
            if (Current == this) Current = null;
            if (_camera != null)
            {
                _camera.targetTexture = null;
                Destroy(_camera.gameObject);
            }
            if (_rt != null)
            {
                _rt.Release();
                Destroy(_rt);
            }
            _rig?.Dispose();
            _rig = null;
        }
    }

    /// <summary>
    /// 一個 3D 角色的**執行期組裝**（載模型 → 收掉道具 → 接材質 → 放下手臂 → 待機動作）。
    /// 與 MonoBehaviour 分開：編輯器的無頭預覽（`Stage3DPreview`）走同一支，不必進播放模式。
    /// </summary>
    public sealed class Stage3DRig
    {
        /// <summary>卡通著色（`Assets/Shaders/Stage3DToon.shader`；兩階明暗＋邊緣光＋描邊）。</summary>
        public const string ToonShader = "BlueAgent/Stage3DToon";

        /// <summary>光從哪來（相機座標：左上、偏前）。</summary>
        private static readonly Vector3 LightFromCamera = new Vector3(-0.45f, 0.65f, -0.6f);
        private static readonly int LightDirId = Shader.PropertyToID("_Stage3DLightDir");

        /// <summary>
        /// 嘴型圖集（`Character_Mouth`，8×8 格、每格一種嘴）裡要用哪一格：(列, 欄)，列從圖的上面數。
        /// 平常＝微笑線；thinking＝一字嘴；speaking＝幾種開合輪流（簡易的「嘴在動」，不是真的對嘴型）。
        /// </summary>
        private static readonly Vector2Int MouthRest = new Vector2Int(0, 4);
        private static readonly Vector2Int MouthThink = new Vector2Int(3, 1);
        private static readonly Vector2Int[] MouthTalk =
        {
            new Vector2Int(0, 5), new Vector2Int(0, 0), new Vector2Int(0, 6), new Vector2Int(0, 4),
            new Vector2Int(0, 0), new Vector2Int(0, 5), new Vector2Int(0, 4), new Vector2Int(0, 6),
        };
        private const int MouthGrid = 8;

        /// <summary>照語音音量開合的四格（第 0 列：閉著的笑線 → 小開 → 開口笑 → 大「o」）。</summary>
        private static readonly Vector2Int[] MouthByOpenness =
        {
            new Vector2Int(0, 6), new Vector2Int(0, 5), new Vector2Int(0, 7), new Vector2Int(0, 0),
        };
        /// <summary>開合的門檻（相對最近的大聲程度，0–1）；往下要多掉一點才換回去（遲滯，免得一格一格抖）。</summary>
        private static readonly float[] MouthSteps = { 0.18f, 0.45f, 0.75f };
        private const float MouthHysteresis = 0.06f;
        /// <summary>最近的大聲程度：每秒衰減到這個比例（大約兩三秒忘掉一次大聲），下限避免小聲時被放大成大嘴。</summary>
        private const float LoudnessDecayPerSecond = 0.6f;
        private const float LoudnessFloor = 0.04f;

        private float _loudness = LoudnessFloor;
        private int _openStep;

        /// <summary>
        /// 音量 → 嘴型（owner 2026-10-31：以前說話時只是固定節奏輪換，跟聲音對不上）。
        /// 音量先除以「最近最大聲」做正規化：不同角色、不同音量設定都會用到整個開合範圍。
        /// </summary>
        private Vector2Int MouthForLevel(float level, float dt)
        {
            _loudness = Mathf.Max(level, Mathf.Max(LoudnessFloor, _loudness * Mathf.Pow(LoudnessDecayPerSecond, dt)));
            var openness = Mathf.Clamp01(level / _loudness);
            while (_openStep < MouthSteps.Length && openness >= MouthSteps[_openStep]) _openStep++;
            while (_openStep > 0 && openness < MouthSteps[_openStep - 1] - MouthHysteresis) _openStep--;
            return MouthByOpenness[_openStep];
        }

        /// <summary>
        /// 嘴那一塊在眼嘴層 UV 裡的位置（三個角色同一塊：glTF 的 u 0.074–0.176、v 0.793–0.851；
        /// Unity 的 v 是上下翻過的，兩種方向都認）。框住的三角形整片換成嘴型圖集。
        /// </summary>
        private static readonly Rect[] MouthBoxes =
        {
            Rect.MinMaxRect(0.06f, 0.13f, 0.19f, 0.22f),
            Rect.MinMaxRect(0.06f, 0.78f, 0.19f, 0.87f),
        };

        /// <summary>每幀把光擺到相機的左上前方（世界座標；所有 3D 立繪共用一個全域值）。</summary>
        public static void Light(Camera camera)
        {
            if (camera == null) return;
            Shader.SetGlobalVector(LightDirId, camera.transform.TransformDirection(LightFromCamera.normalized));
        }

        public static string Folder(string character) => "models3d/" + character + "/";

        private const float FieldOfView = 18f;
        private const float Padding = 1.06f;

        /// <summary>
        /// 取景要拍到身體的多少（從頭頂往下量；1＝整個人）。owner 2026-10-31「完整模型看起來有點偏小」，
        /// 看過 100／85／72／60 四檔的比較後選 0.72（Q 版頭大：大約拍到腰下，半身像）。
        /// 預覽可用 `BA_STAGE3D_SHOW` 比較不同值。
        /// </summary>
        public const float DefaultShow = 0.72f;

        /// <summary>模型相機：只看模型層、clear 成透明（模型以外透出舞台背景）、窄視角（近似正交、透視不誇張）。</summary>
        public static Camera NewCamera(string name, int layer)
        {
            var camera = new GameObject(name).AddComponent<Camera>();
            camera.cullingMask = 1 << layer;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0f, 0f, 0f, 0f);
            camera.fieldOfView = FieldOfView;
            camera.nearClipPlane = 0.05f;
            camera.farClipPlane = 50f;
            camera.allowHDR = false;
            return camera;
        }

        /// <summary>
        /// 相機框住人（bounds 在載入時量一次就固定，待機的晃動不會讓取景跟著抖）。
        /// <paramref name="show"/>＝從頭頂往下拍到身體的幾成（1＝整個人；&lt;1 就是從頭到大腿／膝蓋的半身）。
        /// </summary>
        public void Aim(Camera camera, float aspect, float show = DefaultShow)
        {
            show = Mathf.Clamp(show, 0.3f, 1f);
            var tan = Mathf.Tan(camera.fieldOfView * 0.5f * Mathf.Deg2Rad);
            // 只看「沿著鏡頭方向」的厚度（以前拿寬度 x 也算進去，人一寬就整個退後）
            var depth = Mathf.Abs(Bounds.extents.x * Forward.x) + Mathf.Abs(Bounds.extents.z * Forward.z);
            var halfHeight = Bounds.extents.y * show;
            var halfWidth = Mathf.Max(Bounds.extents.x, Bounds.extents.z);
            var distance = Mathf.Max(halfHeight * Padding / tan, halfWidth * Padding / (tan * Mathf.Max(0.2f, aspect)))
                           + depth;
            // 畫面的中心：頭頂往下 halfHeight（整個人時就是 bounds 中心）
            var center = new Vector3(Bounds.center.x, Bounds.max.y - halfHeight, Bounds.center.z);
            camera.transform.position = center + Forward * distance;
            camera.transform.rotation = Quaternion.LookRotation(-Forward, Vector3.up);
        }

        public GameObject Root { get; private set; }
        /// <summary>載入時量的世界 bounds（手臂放下之後；只量留下來的網格）。</summary>
        public Bounds Bounds { get; private set; }
        /// <summary>角色正面朝向（世界座標；相機擺在這一側）。</summary>
        public Vector3 Forward { get; private set; }

        private readonly List<Material> _materials = new List<Material>();
        /// <summary>嘴移到自己子網格之後的網格複本（歸這個 rig 所有）。</summary>
        private Mesh _ownedMesh;
        private Material _mouth;
        private Stage3DSpring _spring;
        private Stage3DMotion _motion;
        /// <summary>MAGIC 待機（null＝沒有資料或擬合失敗 ⇒ 用固定正弦）。</summary>
        private MagicIdlePlayer _idle;

        // ── MAGIC 待機模型（`docs/PLAN-magic-motion-20261031.md`）：全 App 擬合一次，每個 rig 各自一條生成流 ──
        private const string IdleData = "models3d/magic-idle";
        private const int IdleOrder = 12;
        private const double IdleRidge = 0.001;
        private const int IdleArHmmOrder = 6;
        private const int IdleArHmmStates = 3;
        private const int IdleArHmmIterations = 3;
        /// <summary>AR-HMM 待機的每軸角速度上限（度／秒）：安全網，避免一幀衝太遠。</summary>
        private const double IdleMaxDegreesPerSecond = 240.0;
        /// <summary>
        /// VAR 待機（預設）的每軸角速度上限（度／秒）。VAR 偶爾連續幾個取樣噴大雜訊 ⇒ 頭一幀甩好幾度
        /// （2026-11-01 無頭量 60 秒：沒限速時頭相對身體最快 538°/s、15 幀超過 90°/s；用 AR-HMM 那個 240 也還有 390）。
        /// 偏移本來就夾在 ±<see cref="IdleMaxOffsetDegrees"/>，45°/s 走完整個範圍也只要半秒，不會拖。手機舞台同一支。
        /// </summary>
        private const double IdleVarMaxDegreesPerSecond = 45.0;
        /// <summary>換狀態時的慣性化時間常數（秒）：跳變量約 0.35 秒衰減到 1/e。</summary>
        private const double IdleTransitionSeconds = 0.5;
        /// <summary>待機偏移上限（度）：超過就夾住，避免忽然換姿勢。</summary>
        private const double IdleMaxOffsetDegrees = 12.0;
        private static bool _idleLoaded;
        private static VarModel _idleModel;
        private static ArHmmModel _idleArHmmModel;
        /// <summary>AR-HMM 擬合失敗過 ⇒ 這次 App 不再重試（每次換角色都重擬合一遍很貴，也會洗 log）。</summary>
        private static bool _idleArHmmFailed;
        private static string[] _idleAxes;

        private static VarModel IdleModel(out string[] axes)
        {
            if (!_idleLoaded)
            {
                _idleLoaded = true;
                try
                {
                    var asset = Resources.Load<TextAsset>(IdleData);
                    if (asset != null)
                    {
                        var started = Time.realtimeSinceStartup;
                        var data = MagicIdleData.Parse(asset.text);
                        _idleModel = VarModel.Fit(data.Sequence, IdleOrder, IdleRidge);
                        _idleAxes = data.Axes;
                        Debug.Log($"[next] 3D 待機 MAGIC：{data.Sequence.Frames.Count} 幀 × {data.Axes.Length} 軸，" +
                                  $"擬合 {(Time.realtimeSinceStartup - started) * 1000f:0} ms（{data.Source}）");
                    }
                }
                catch (Exception error)
                {
                    _idleModel = null;
                    Debug.LogWarning("[next] 3D 待機 MAGIC 載入失敗，退回正弦：" + error.Message);
                }
            }
            axes = _idleAxes;
            return _idleModel;
        }

        /// <summary>
        /// AR-HMM 待機模型（實驗）：同一份 `magic-idle.json`，但用 EM 切成幾個動作基元再生成。
        /// 比 VAR 重（階數 6、3 狀態、3 次迭代），所以在手機上會慢一點 ⇒ 量一次耗時記在 log 裡。
        /// </summary>
        private static ArHmmModel IdleArHmmModel()
        {
            if (_idleArHmmModel != null) return _idleArHmmModel;
            if (_idleArHmmFailed) return null;
            try
            {
                var asset = Resources.Load<TextAsset>(IdleData);
                if (asset == null) return null;
                var started = Time.realtimeSinceStartup;
                var data = MagicIdleData.Parse(asset.text);
                _idleAxes = data.Axes;
                _idleArHmmModel = ArHmmModel.Fit(data.Sequence, new ArHmmOptions
                {
                    StateCount = IdleArHmmStates,
                    Order = IdleArHmmOrder,
                    Ridge = IdleRidge,
                    Iterations = IdleArHmmIterations,
                    // 待機不該一直換姿勢：對角線加大 ⇒ 停留久一點（上游 2，這裡 10）
                    SelfTransitionBias = 10.0,
                });
                var diagnostics = _idleArHmmModel.Diagnostics;
                Debug.Log($"[next] 3D 待機 AR-HMM：擬合 {(Time.realtimeSinceStartup - started) * 1000f:0} ms，"
                          + $"狀態 {diagnostics.StateCount}、平均停留 {diagnostics.MeanDwellSeconds:0.00}s");
                return _idleArHmmModel;
            }
            catch (Exception error)
            {
                _idleArHmmFailed = true;
                Debug.LogWarning("[next] 3D 待機 AR-HMM 失敗，退回 VAR：" + error.Message);
                return null;
            }
        }

        /// <summary>播一段動作（後端 Kimodo 的 `bamotion/1`）；播完自己淡回待機。</summary>
        public void PlayMotion(MotionClip clip) => _motion?.Play(clip);

        public bool MotionPlaying => _motion != null && _motion.Playing;
        private Vector2Int _mouthTile = new Vector2Int(-1, -1);
        private readonly List<KeyValuePair<Transform, Quaternion>> _rest = new List<KeyValuePair<Transform, Quaternion>>();
        private Transform _spine;
        private Transform _chest;
        private Transform _neck;
        private Transform _head;
        // 角色自己的軸（Root 座標系；轉身時跟著 Root 轉）
        private Vector3 _rightLocal;
        private Vector3 _forwardLocal;
        private Quaternion _rootRest;

        /// <summary>腳趾（沒有就腳踝）骨頭：量「腳底現在多高」用（MR 的貼地判據）。</summary>
        private readonly List<Transform> _toes = new List<Transform>();
        /// <summary>載入時腳趾骨頭比網格最低點高多少（世界單位，已含縮放）。</summary>
        private float _soleBelowToe;

        /// <summary>
        /// 腳底現在的世界高度：腳趾骨頭最低的那根 − 載入時量的「骨頭到鞋底」。跟著待機／動作的重心移動走，
        /// 不是載入那一刻的 bounds。沒有腳骨就退回 bounds 的底。
        /// </summary>
        public float SoleY
        {
            get
            {
                var lowest = float.MaxValue;
                foreach (var toe in _toes)
                {
                    if (toe != null) lowest = Mathf.Min(lowest, toe.position.y);
                }
                return lowest == float.MaxValue ? Bounds.min.y : lowest - _soleBelowToe;
            }
        }

        /// <summary>
        /// <paramref name="height"/>＞0 ⇒ 把整個人（含光環）縮放到這個高度（公尺；MR 的真實尺度）。
        /// 縮放在建骨架／物理／動作**之前**做 ⇒ 它們量到的長度一開始就是縮過的。0＝模型原尺寸（手機舞台）。
        /// </summary>
        public static Stage3DRig Spawn(string character, Vector3 at, int layer, float height = 0f)
        {
            var folder = Folder(character);
            var table = Resources.Load<TextAsset>(folder + "stage3d");
            if (table == null) throw new InvalidOperationException("missing " + folder + "stage3d.json");
            var prefab = Resources.Load<GameObject>(folder + "model");
            if (prefab == null) throw new InvalidOperationException("missing " + folder + "model (FBX not imported?)");
            var shader = Shader.Find(ToonShader);
            if (shader == null) throw new InvalidOperationException("shader not found: " + ToonShader + " (not in Always Included?)");

            var json = new JObject(table.text);
            var keep = Names(json.OptJArray("keep"));
            var textures = json.OptJObject("textures") ?? new JObject();
            var mouth = json.OptJObject("mouth");
            var mouthLayer = mouth != null ? mouth.OptString("layer") : "";

            var rig = new Stage3DRig();
            rig.Root = new GameObject("Stage3D_" + character);
            rig.Root.transform.position = at;
            var model = UnityEngine.Object.Instantiate(prefab, rig.Root.transform, false);
            model.name = "Model";

            var kept = new List<Renderer>();
            foreach (var renderer in model.GetComponentsInChildren<Renderer>(true))
            {
                if (!keep.Contains(renderer.name))
                {
                    renderer.enabled = false;            // 武器、EX 道具、FX：不畫也不量
                    continue;
                }
                kept.Add(renderer);
                if (renderer is SkinnedMeshRenderer skinned) skinned.updateWhenOffscreen = true;
                var slots = renderer.sharedMaterials;
                var replaced = new List<Material>(slots.Length + 1);
                var mouthSlot = -1;
                for (var i = 0; i < slots.Length; i++)
                {
                    var name = slots[i] != null ? slots[i].name : renderer.name;
                    var textureName = textures.OptString(name, name);
                    var texture = Resources.Load<Texture2D>(folder + textureName);
                    if (texture == null) Debug.LogWarning($"[next] 3D 貼圖讀不到：{folder}{textureName}（材質 {name}）");
                    replaced.Add(rig.NewMaterial(shader, name, texture, Flat(name)));
                    if (name == mouthLayer) mouthSlot = i;
                }
                if (mouthSlot >= 0 && renderer is SkinnedMeshRenderer face)
                {
                    var atlas = Resources.Load<Texture2D>("models3d/" + mouth.OptString("atlas"));
                    if (atlas == null) Debug.LogWarning("[next] 3D 嘴型圖集讀不到：" + mouth.OptString("atlas"));
                    else if (rig.SplitMouth(face, mouthSlot))
                    {
                        rig._mouth = rig.NewMaterial(shader, "Mouth", atlas, true);
                        rig._mouth.SetFloat("_Cutoff", 0.35f);
                        rig._mouth.renderQueue = 2450;                 // 疊在臉上（AlphaTest 那一段）
                        rig._mouth.SetTextureScale("_BaseMap", Vector2.one / MouthGrid);
                        replaced.Add(rig._mouth);
                    }
                }
                renderer.sharedMaterials = replaced.ToArray();
            }
            if (kept.Count == 0)
            {
                rig.Dispose();
                throw new InvalidOperationException("no kept renderers (stage3d.json keep does not match node names)");
            }

            if (height > 0f)
            {
                var native = Measure(kept).size.y;
                if (native > 1e-4f)
                {
                    var scale = height / native;
                    rig.Root.transform.localScale = Vector3.one * scale;
                    // 描邊寬是世界單位（公尺）寫死的 ⇒ 跟著縮，桌上 30 cm 的她才不會描得比手機上粗四倍
                    foreach (var material in rig._materials)
                        material.SetFloat("_OutlineWidth", material.GetFloat("_OutlineWidth") * scale);
                }
            }

            SetLayer(rig.Root, layer);
            rig.Rig(model.transform, Names(json.OptJArray("attachHead")));
            SkinnedMeshRenderer body = null;
            foreach (var renderer in kept)
            {
                if (renderer is SkinnedMeshRenderer skinned && (body == null || skinned.bones.Length > body.bones.Length))
                    body = skinned;
            }
            rig._spring = Stage3DSpring.Build(model.transform, body);
            Debug.Log($"[next] 3D 物理：{rig._spring.JointCount} 根骨頭、{rig._spring.ColliderCount} 顆碰撞球");
            rig.Bounds = Measure(kept);
            if (rig._toes.Count > 0)
            {
                var toe = float.MaxValue;
                foreach (var bone in rig._toes) toe = Mathf.Min(toe, bone.position.y);
                rig._soleBelowToe = toe - rig.Bounds.min.y;
            }
            rig.SetMouth(MouthRest);
            return rig;
        }

        /// <summary>臉的幾層與光環：不打陰影、不描邊（二次元的臉是平塗的；光環是發光體）。</summary>
        private static bool Flat(string material) =>
            material.EndsWith("_Face") || material.Contains("_Eyebrow") || material.EndsWith("_EyeMouth")
            || material.EndsWith("_Halo");

        private Material NewMaterial(Shader shader, string name, Texture2D texture, bool flat)
        {
            var material = new Material(shader) { name = name };
            material.SetTexture("_BaseMap", texture);
            material.SetColor("_BaseColor", Color.white);
            material.SetFloat("_Cull", 0f);       // BA 的網格是雙面材質（裙子、頭髮內側）
            if (flat)
            {
                material.SetFloat("_ShadeStrength", 0f);
                material.SetFloat("_RimStrength", 0f);
                material.SetFloat("_OutlineWidth", 0f);
            }
            _materials.Add(material);
            return material;
        }

        /// <summary>
        /// 把眼嘴層裡**嘴那一塊**（UV 落在 <see cref="MouthBoxes"/> 的三角形）搬到自己的子網格，
        /// UV 拉成 0–1，交給嘴型圖集那個材質畫（換表情＝改材質的 tiling offset，網格不動）。
        /// 為什麼：BA 的 shader 用參數在共用圖集 `Character_Mouth` 裡選格；這裡沒有那支 shader，
        /// 嘴那塊照原 UV 會取到角色自己的眼嘴貼圖 ⇒ 一片白（2026-10-30 無頭預覽三個角色都是）。
        /// </summary>
        private bool SplitMouth(SkinnedMeshRenderer renderer, int submesh)
        {
            var source = renderer.sharedMesh;
            if (source == null || !source.isReadable || submesh >= source.subMeshCount) return false;
            var uvs = new List<Vector2>();
            source.GetUVs(0, uvs);
            var triangles = source.GetTriangles(submesh);
            foreach (var box in MouthBoxes)
            {
                var keep = new List<int>(triangles.Length);
                var mouth = new List<int>();
                for (var t = 0; t + 2 < triangles.Length; t += 3)
                {
                    var inside = box.Contains(uvs[triangles[t]]) && box.Contains(uvs[triangles[t + 1]])
                                 && box.Contains(uvs[triangles[t + 2]]);
                    (inside ? mouth : keep).AddRange(new[] { triangles[t], triangles[t + 1], triangles[t + 2] });
                }
                if (mouth.Count == 0) continue;
                var dropped = DropAroundMouth(source, renderer.transform.localToWorldMatrix, mouth, keep);

                var min = new Vector2(float.MaxValue, float.MaxValue);
                var max = new Vector2(float.MinValue, float.MinValue);
                foreach (var v in mouth)
                {
                    min = Vector2.Min(min, uvs[v]);
                    max = Vector2.Max(max, uvs[v]);
                }
                var size = Vector2.Max(max - min, new Vector2(1e-5f, 1e-5f));
                var flip = box.yMin > 0.5f;                 // glTF 方向（v 朝下）⇒ 上下翻回來
                var done = new HashSet<int>();
                foreach (var v in mouth)
                {
                    if (!done.Add(v)) continue;
                    var local = new Vector2((uvs[v].x - min.x) / size.x, (uvs[v].y - min.y) / size.y);
                    if (flip) local.y = 1f - local.y;
                    uvs[v] = local;
                }

                var mesh = UnityEngine.Object.Instantiate(source);
                mesh.name = source.name;
                mesh.SetUVs(0, uvs);
                mesh.subMeshCount = source.subMeshCount + 1;
                mesh.SetTriangles(keep, submesh);
                mesh.SetTriangles(mouth, source.subMeshCount);
                renderer.sharedMesh = mesh;
                _ownedMesh = mesh;
                Debug.Log($"[next] 3D 嘴：{mouth.Count / 3} 個三角形換成嘴型圖集（UV {min}–{max}）；嘴邊拿掉 {dropped} 個");
                return true;
            }
            Debug.LogWarning("[next] 3D 嘴：眼嘴層裡找不到嘴那一塊（UV 對不上）⇒ 不換");
            return false;
        }

        /// <summary>
        /// 眼嘴層裡**嘴附近的其他三角形**也拿掉：它們在 BA 的 shader 裡是被遮罩掉的（張嘴的口腔、預設嘴形），
        /// 這裡沒有遮罩，只會照角色自己的眼嘴貼圖畫出一塊粉／白（2026-10-31 臉部特寫實測）。
        /// 範圍＝重心低於「嘴那塊的中心再往上一個嘴高」——眼睛的下緣在那之上將近兩個嘴高，碰不到。
        /// </summary>
        private static int DropAroundMouth(Mesh source, Matrix4x4 toWorld, List<int> mouth, List<int> keep)
        {
            var vertices = source.vertices;
            float low = float.MaxValue, high = float.MinValue;
            foreach (var v in mouth)
            {
                var y = toWorld.MultiplyPoint3x4(vertices[v]).y;
                low = Mathf.Min(low, y);
                high = Mathf.Max(high, y);
            }
            var limit = (low + high) * 0.5f + (high - low);
            var kept = new List<int>(keep.Count);
            var dropped = 0;
            for (var t = 0; t + 2 < keep.Count; t += 3)
            {
                var centroid = (toWorld.MultiplyPoint3x4(vertices[keep[t]]).y
                                + toWorld.MultiplyPoint3x4(vertices[keep[t + 1]]).y
                                + toWorld.MultiplyPoint3x4(vertices[keep[t + 2]]).y) / 3f;
                if (centroid < limit) { dropped++; continue; }
                kept.Add(keep[t]);
                kept.Add(keep[t + 1]);
                kept.Add(keep[t + 2]);
            }
            keep.Clear();
            keep.AddRange(kept);
            return dropped;
        }

        /// <summary>嘴換成圖集裡的某一格（列從圖的上面數；同一格就不動）。</summary>
        private void SetMouth(Vector2Int tile)
        {
            if (_mouth == null || tile == _mouthTile) return;
            _mouthTile = tile;
            _mouth.SetTextureOffset("_BaseMap",
                new Vector2((float)tile.y / MouthGrid, 1f - (tile.x + 1f) / MouthGrid));
        }

        /// <summary>找骨頭、把手臂放下、光環掛到頭上、記下靜止姿勢。</summary>
        private void Rig(Transform model, HashSet<string> attachHead)
        {
            var pelvis = Bone(model, "Bip001 Pelvis");
            _spine = Bone(model, "Bip001 Spine");
            _chest = Bone(model, "Bip001 Spine1");
            _neck = Bone(model, "Bip001 Neck");
            _head = Bone(model, "Bip001 Head");
            var upperL = Bone(model, "Bip001 L UpperArm");
            var upperR = Bone(model, "Bip001 R UpperArm");
            var foreL = Bone(model, "Bip001 L Forearm");
            var foreR = Bone(model, "Bip001 R Forearm");

            // 角色的「前」＝腳跟指向腳尖（兩腳平均）。⚠️ 不能用 Biped 的 L／R 推：FBX 轉檔把左右鏡像了
            // （2026-10-30 無頭預覽實測：照 L→R 推出來的「前」三個角色都是背面），腳尖方向不受鏡像影響。
            var forward = Vector3.zero;
            foreach (var side in new[] { "L", "R" })
            {
                var foot = Bone(model, $"Bip001 {side} Foot");
                var toe = Bone(model, $"Bip001 {side} Toe0");
                if (foot != null && toe != null) forward += Vector3.ProjectOnPlane(toe.position - foot.position, Vector3.up);
            }
            if (forward.sqrMagnitude < 1e-6f) forward = Vector3.forward;
            Forward = forward.normalized;
            var right = Vector3.Cross(Vector3.up, Forward).normalized;
            _rootRest = Root.transform.rotation;
            _rightLocal = Root.transform.InverseTransformDirection(right);
            _forwardLocal = Root.transform.InverseTransformDirection(Forward);

            // 動作重定向要記 T-pose 的世界旋轉 ⇒ 一定要在放下手臂**之前**建
            var feet = float.MaxValue;
            foreach (var side in new[] { "L", "R" })
            {
                var toe = FindDeep(model, $"Bip001 {side} Toe0") ?? FindDeep(model, $"Bip001 {side} Foot");
                if (toe == null) continue;
                feet = Mathf.Min(feet, toe.position.y);
                _toes.Add(toe);
            }
            if (feet == float.MaxValue) feet = model.position.y;
            _motion = Stage3DMotion.Build(Root.transform, model, Forward, right, FindDeep, feet);

            // T-pose → 手臂垂到身側（往外留一點、略往前，免得穿進裙子）；「外」看手臂在身體哪一側，不靠 L／R 名字
            var center = pelvis != null ? pelvis.position : model.position;
            LowerArm(upperL, foreL, Outward(upperL, center, right));
            LowerArm(upperR, foreR, Outward(upperR, center, right));

            foreach (var name in attachHead)
            {
                var node = FindDeep(model, name);
                if (node != null && _head != null) node.SetParent(_head, true);
            }

            // 每幀放回的靜止姿勢：待機會轉的那幾根＋動作會寫的全部（手臂、腿……放下手臂之後的樣子）
            var resting = new HashSet<Transform>();
            foreach (var bone in new[] { pelvis, _spine, _chest, _neck, _head })
            {
                if (bone != null && resting.Add(bone)) _rest.Add(new KeyValuePair<Transform, Quaternion>(bone, bone.localRotation));
            }
            foreach (var bone in _motion.Bones)
            {
                if (resting.Add(bone)) _rest.Add(new KeyValuePair<Transform, Quaternion>(bone, bone.localRotation));
            }

            EnsureIdle();
        }

        /// <summary>現在的待機播放器是用哪個模型建的（null＝還沒建）。</summary>
        private bool? _idleIsArHmm;

        /// <summary>
        /// 照設定建（或換）待機播放器。每幀叫：設定頁打開「待機小動作」、或在 VAR／AR-HMM 之間切換，
        /// **不用重進舞台**就生效（2026-10-31 複檢：以前只在 rig 建好那一刻看一次設定）。模型是全 App 共用的快取，換的成本只有建播放器。
        /// </summary>
        private void EnsureIdle()
        {
            if (!NextConfig.IdleMotion) return;
            var wantArHmm = NextConfig.IdleArHmm;
            if (_idleIsArHmm == wantArHmm) return;              // 這個設定已經建過（或確定建不起來）
            var seed = (uint)UnityEngine.Random.Range(1, int.MaxValue);
            MagicIdlePlayer player = null;
            if (wantArHmm)
            {
                var arModel = IdleArHmmModel();
                if (arModel != null)
                {
                    player = new MagicIdlePlayer(arModel, _idleAxes, seed)
                    {
                        // AR-HMM 換狀態時會突然跳到另一個姿勢（owner 2026-10-31「動作跳躍」）：
                        // 慣性化＝把跳變量記成偏移再衰減 ⇒ 從舊姿勢滑進新姿勢；
                        // 限速留著當安全網（幅度大時也不會一幀衝過去）
                        TransitionSeconds = IdleTransitionSeconds,
                        MaxDegreesPerSecond = IdleMaxDegreesPerSecond,
                    };
                }
            }
            if (player == null)
            {
                var varModel = IdleModel(out var idleAxes);
                if (varModel != null)
                    player = new MagicIdlePlayer(varModel, idleAxes, seed) { MaxDegreesPerSecond = IdleVarMaxDegreesPerSecond };
            }
            if (player == null)
            {
                _idleIsArHmm = wantArHmm;                       // 兩個都建不起來：不要每幀重試
                return;
            }
            // 待機不是表演：把大位移夾掉，只剩小幅晃動（owner 2026-10-31）。**VAR 也要夾**：
            // Kimodo 待機資料的 bodyY／headY 會到 ±27°（約 4% 的幀超過 12°；2026-10-31 複檢）
            player.MaxOffsetDegrees = IdleMaxOffsetDegrees;
            _idle = player;
            // 記「設定要的」而不是「實際建成的」：AR-HMM 擬合失敗退回 VAR 時，才不會每幀重建
            _idleIsArHmm = wantArHmm;
        }

        private static Vector3 Outward(Transform upper, Vector3 center, Vector3 right) =>
            upper != null && Vector3.Dot(upper.position - center, right) < 0f ? -right : right;

        private void LowerArm(Transform upper, Transform fore, Vector3 outward)
        {
            if (upper == null || fore == null) return;
            var current = fore.position - upper.position;
            var target = (Vector3.down + outward * 0.22f + Forward * 0.06f).normalized;
            upper.rotation = Quaternion.FromToRotation(current, target) * upper.rotation;
        }

        /// <summary>每幀：回到靜止姿勢，再疊呼吸／晃動／頭部（thinking 歪頭、speaking 點頭）。</summary>
        public void Animate(float time, float dt, float yaw, float thinking, float speaking, float voiceLevel = -1f)
        {
            if (Root == null) return;
            Root.transform.rotation = Quaternion.AngleAxis(yaw, Vector3.up) * _rootRest;
            foreach (var pair in _rest) pair.Key.localRotation = pair.Value;

            var right = Root.transform.TransformDirection(_rightLocal);
            var forward = Root.transform.TransformDirection(_forwardLocal);
            var up = Root.transform.up;

            var neck = _neck != null ? _neck : _head;
            if (NextConfig.IdleMotion) EnsureIdle();
            if (_idle != null && NextConfig.IdleMotion)
            {
                // MAGIC：統計模型生成、不重複的待機（重心、呼吸、看旁邊）；頸＋頭那 3 軸分給 Neck 40%／Head 60%
                _idle.Advance(dt);
                Turn(_spine, right, Idle("bodyX"));
                Turn(_spine, up, Idle("bodyY"));
                Turn(_spine, forward, Idle("bodyZ"));
                Turn(_chest, right, Idle("chestX"));
                Turn(neck, right, Idle("headX") * 0.4f);
                Turn(neck, up, Idle("headY") * 0.4f);
                Turn(neck, forward, Idle("headZ") * 0.4f);
                Turn(_head, right, Idle("headX") * 0.6f);
                Turn(_head, up, Idle("headY") * 0.6f);
                Turn(_head, forward, Idle("headZ") * 0.6f);
            }
            else
            {
                Turn(_spine, right, Mathf.Sin(time * 1.6f) * 1.2f);                 // 呼吸
                Turn(_spine, forward, Mathf.Sin(time * 0.5f) * 1.5f);               // 身體微晃
                Turn(_chest, right, Mathf.Sin(time * 1.6f + 0.6f) * 0.8f);
                Turn(neck, up, Mathf.Sin(time * 0.37f) * 5f);
                Turn(_head, right, Mathf.Sin(time * 0.29f) * 2f);
                Turn(_head, forward, Mathf.Sin(time * 0.23f) * 2f);
            }
            // 狀態疊在待機上：思考歪頭、說話點頭
            Turn(_head, right, -thinking * 4f + Mathf.Sin(time * 8.5f) * 2.2f * speaking);
            Turn(_head, forward, thinking * 9f);

            // 動作（有在播才寫）：片段的目標跟現在的姿勢無關、按權重 Slerp ⇒ 播放中待機自然被蓋掉、播完淡回
            _motion?.Apply(dt);
            Look(dt);

            SetMouth(voiceLevel >= 0f ? MouthForLevel(voiceLevel, dt)
                     : speaking > 0.5f ? MouthTalk[(int)(time * 9f) % MouthTalk.Length]
                     : thinking > 0.5f ? MouthThink : MouthRest);
            _spring?.Step(dt, Root.transform);                  // 頭髮／袖子：主要骨頭擺好之後才算（跟著頭、手臂、轉身甩）
        }

        private float Idle(string axis) => (float)_idle.Offset(axis);

        /// <summary>
        /// 看向哪（世界座標；null＝不看，照待機）。MR：你的頭（PLAN-mr-quest3 M2）。手機舞台不設 ⇒ 這一層完全不動。
        /// </summary>
        public Vector3? LookTarget;
        /// <summary>現在頭（頸＋頭合計）相對身體轉了多少（左右／上下，度；正＝往她的左／往上）。儀表量「她轉頭了沒」用。</summary>
        public float LookYaw => _lookYaw * _lookWeight;
        public float LookPitch => _lookPitch * _lookWeight;
        /// <summary>頭骨（儀表量抖動用）。</summary>
        public Transform HeadBone => _head;

        private float _fade = 1f;

        /// <summary>
        /// 整個人的不透明度（0–1；toon shader 的 `_Fade`）。MR 召喚時淡入淡出（PLAN-mr-quest3 M4）；
        /// 手機舞台不叫 ⇒ 恆為 1。只在值變了才寫材質。
        /// </summary>
        public void SetFade(float fade)
        {
            fade = Mathf.Clamp01(fade);
            if (Mathf.Approximately(fade, _fade)) return;
            _fade = fade;
            foreach (var material in _materials) material.SetFloat("_Fade", fade);
        }

        /// <summary>轉頭的上限（度）：再多就像貓頭鷹；超過時由外面（MrShell）轉身。</summary>
        public const float LookYawLimit = 70f;
        private const float LookUpLimit = 40f;
        private const float LookDownLimit = 25f;
        /// <summary>
        /// 頭最快轉多快（度／秒）。目標一下從她左邊跳到右邊（你從她背後繞過去）時，平滑本身會先甩出幾百度／秒，像抽筋；
        /// 封頂之後是「轉過來」而不是「甩過來」（2026-11-01 實機：沒封頂時 60 秒最大 228°/s）。
        /// </summary>
        private const float LookMaxSpeed = 150f;
        /// <summary>
        /// 你離她頭的水平距離小於這個（公尺）＝幾乎在她正上方：左右方向沒有意義（移一點點就差上百度），只追上下、左右不動。
        /// </summary>
        private const float LookOverhead = 0.2f;
        private float _lookWeight;
        private float _lookYaw;
        private float _lookPitch;

        /// <summary>
        /// 疊在待機與動作**之後**：頸 40%、頭 60%（跟待機同一個分法），先左右、再沿著轉過去之後的「右」軸上下。
        /// 角度往目標平滑追（不瞬間扭頭）；有目標淡入、沒目標淡出。播動作時只用一半（動作自己也在帶頭）。
        /// </summary>
        private void Look(float dt)
        {
            _lookWeight = Mathf.MoveTowards(_lookWeight, LookTarget.HasValue ? 1f : 0f, dt * 2f);
            if (_lookWeight <= 0f || _head == null) return;
            var up = Vector3.up;
            var facing = Vector3.ProjectOnPlane(Root.transform.rotation * Forward, up).normalized;
            if (LookTarget.HasValue)
            {
                var to = LookTarget.Value - _head.position;
                var flat = Vector3.ProjectOnPlane(to, up);
                if (flat.sqrMagnitude > 1e-6f)
                {
                    var yaw = Mathf.Clamp(Vector3.SignedAngle(facing, flat, up), -LookYawLimit, LookYawLimit);
                    var pitch = Mathf.Clamp(Mathf.Atan2(to.y, flat.magnitude) * Mathf.Rad2Deg, -LookDownLimit, LookUpLimit);
                    var follow = 1f - Mathf.Exp(-6f * dt);
                    var maxStep = LookMaxSpeed * dt;
                    if (flat.magnitude > LookOverhead)
                        _lookYaw = Mathf.MoveTowards(_lookYaw, Mathf.Lerp(_lookYaw, yaw, follow), maxStep);
                    _lookPitch = Mathf.MoveTowards(_lookPitch, Mathf.Lerp(_lookPitch, pitch, follow), maxStep);
                }
            }
            var weight = _lookWeight * (MotionPlaying ? 0.5f : 1f);
            var neck = _neck != null ? _neck : _head;
            Turn(neck, up, _lookYaw * 0.4f * weight);
            Turn(_head, up, _lookYaw * 0.6f * weight);
            // 繞「右」軸正轉＝低頭 ⇒ 抬頭用負角度（Unity 左手座標）
            var right = Vector3.Cross(up, Quaternion.AngleAxis(_lookYaw * weight, up) * facing).normalized;
            Turn(neck, right, -_lookPitch * 0.4f * weight);
            Turn(_head, right, -_lookPitch * 0.6f * weight);
        }

        private static void Turn(Transform bone, Vector3 axis, float degrees)
        {
            if (bone == null || Mathf.Abs(degrees) < 0.0001f) return;
            bone.rotation = Quaternion.AngleAxis(degrees, axis) * bone.rotation;
        }

        public void Dispose()
        {
            foreach (var material in _materials) Release(material);
            _materials.Clear();
            if (_ownedMesh != null) Release(_ownedMesh);
            _ownedMesh = null;
            if (Root != null) Release(Root);
            Root = null;
        }

        private static void Release(UnityEngine.Object target)
        {
            if (Application.isPlaying) UnityEngine.Object.Destroy(target);
            else UnityEngine.Object.DestroyImmediate(target);
        }

        /// <summary>量 bounds：蒙皮網格用 `BakeMesh`（當下骨架的真實形狀；編輯模式也成立）。</summary>
        private static Bounds Measure(List<Renderer> renderers)
        {
            var bounds = new Bounds();
            var found = false;
            var baked = new Mesh();
            foreach (var renderer in renderers)
            {
                Bounds piece;
                if (renderer is SkinnedMeshRenderer skinned)
                {
                    skinned.BakeMesh(baked, true);
                    piece = Transform(baked.bounds, skinned.transform.localToWorldMatrix);
                }
                else
                {
                    piece = renderer.bounds;
                }
                if (!found) { bounds = piece; found = true; }
                else bounds.Encapsulate(piece);
            }
            Release(baked);
            return bounds;
        }

        private static Bounds Transform(Bounds local, Matrix4x4 matrix)
        {
            var result = new Bounds(matrix.MultiplyPoint3x4(local.center), Vector3.zero);
            for (var i = 0; i < 8; i++)
            {
                var corner = local.center + Vector3.Scale(local.extents,
                    new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
                result.Encapsulate(matrix.MultiplyPoint3x4(corner));
            }
            return result;
        }

        /// <summary>照名字找骨頭（FBX 轉檔有時把空白換成底線，比對時忽略兩者）。</summary>
        private static Transform Bone(Transform root, string name)
        {
            var bone = FindDeep(root, name);
            if (bone == null) Debug.LogWarning($"[next] 3D 骨架裡沒有 {name}");
            return bone;
        }

        private static Transform FindDeep(Transform root, string name)
        {
            var wanted = Normalize(name);
            foreach (var node in root.GetComponentsInChildren<Transform>(true))
            {
                if (Normalize(node.name) == wanted) return node;
            }
            return null;
        }

        private static string Normalize(string name) =>
            (name ?? "").Replace(" ", "").Replace("_", "").ToLowerInvariant();

        private static HashSet<string> Names(JArray array)
        {
            var names = new HashSet<string>();
            for (var i = 0; array != null && i < array.Length(); i++)
            {
                var name = array.OptString(i);
                if (!string.IsNullOrEmpty(name)) names.Add(name);
            }
            return names;
        }

        private static void SetLayer(GameObject go, int layer)
        {
            foreach (var child in go.GetComponentsInChildren<Transform>(true)) child.gameObject.layer = layer;
        }
    }
}
