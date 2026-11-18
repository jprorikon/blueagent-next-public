using System.Collections.Generic;
using System.Globalization;
using BlueAgent.Next;
using TMPro;
using UnityEngine;
using UnityEngine.XR;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace BlueAgent.MR
{
    /// <summary>
    /// M0 的驗收儀表（PLAN-mr-quest3 §3 M0 的 ②③④，**量出來**而不是看起來）：
    /// <list type="bullet">
    /// <item>腳底與所站的面（桌面；沒桌子時是地面）的高差（cm）：從腳底正上方往下打平面，`SoleY − 命中 y`；判據 &lt; 2 cm。</item>
    /// <item>漂移（cm，最近 60 秒的最大值）：她在世界裡不動，所站的平面跟著真實房間走 ⇒ 召喚時記下「她在平面座標系的位置」，
    ///   之後每幀換回世界比距離；判據 1 分鐘 &lt; 3 cm。她**刻意不掛在平面底下**，掛了這個數字就恆為 0、量不到東西。</item>
    /// <item>幀率：最近 1 秒、最近 60 秒平均、60 秒內最慢一幀（ms），旁邊附顯示器刷新率；不預設數字（§4-5）。
    ///   M1 起 log 行多一欄 `occ=on/off`：遮擋開關前後各跑滿 60 秒，就是「遮擋前後幀率差」。</item>
    /// </list>
    /// 數字畫在眼前（跟著頭、慢半拍），每 10 秒也打一行 `[mr] probe …` 進 logcat（`adb logcat -s Unity | grep '\[mr\]'`）。
    ///
    /// owner 2026-11-01：除錯儀表**預設藏起來**（畫面上只留頭頂的聽筒圖示與視野下方的通知 `MrToast`）；
    /// 量測與 logcat 照常跑，手柄按下搖桿（<see cref="Visible"/>）才畫出來。
    /// </summary>
    public sealed class MrProbe : MonoBehaviour
    {
        private const int WindowSeconds = 60;
        private const float LogEvery = 10f;
        /// <summary>儀表在眼前多遠（公尺）。</summary>
        private const float Distance = 1.1f;
        /// <summary>
        /// 儀表在視線**上方**多少（公尺）。她是桌上 30 cm（§5.1）、在你視線下方 ⇒ 儀表放上面，不要擋住她。
        /// </summary>
        private const float Rise = 0.22f;

        /// <summary>畫不畫在眼前（預設不畫；量測與 log 不受影響）。</summary>
        public bool Visible;

        /// <summary>第一行的狀態／警告（i18n key；null＝沒有）。</summary>
        public string StatusKey;

        /// <summary>對話那幾行（橋的狀態、正在聽、你說的、她說的；已經是這一種語言的字）。M3。</summary>
        public string Conversation;

        /// <summary>錨點那一行（已經換成這一種語言的字；null＝沒有）。M1。</summary>
        public string Note;

        /// <summary>遮擋狀態（i18n key：mr_hud_occlusion_on／off／wait；null＝還沒有權限）。M1。</summary>
        public string OcclusionKey;

        /// <summary>你在她身體正前方的哪一邊（度；MrShell 每幀填）。M2：配上她的頭轉了多少，就是「轉頭了沒」的實測。</summary>
        public float Bearing = float.NaN;

        /// <summary>頭骨的角速度（度／秒）：待機／看人時頭有沒有抖（一幀內甩一大下）的量法。M2。</summary>
        private float _headSpeed = float.NaN;
        private Quaternion _lastHead;
        private bool _hasLastHead;
        /// <summary>
        /// 剛召喚的那一秒不量頭速：第一幀從綁定姿勢跳進待機姿勢，算出來幾百度／秒，那是「生出來」不是抖
        /// （2026-11-01 實機：連按 B／Y 重放 18 次，60 秒最大被灌到 532°/s）。
        /// </summary>
        private const float HeadSpeedSettle = 1f;
        private float _trackedAt;

        private Camera _camera;
        private ARRaycastManager _raycasts;
        private TextMeshPro _text;

        private Stage3DRig _rig;
        private ARPlane _surface;
        private float _surfaceY;
        private Vector3 _onPlane;

        private float _gap = float.NaN;
        private float _drift = float.NaN;
        private float _hz = float.NaN;

        /// <summary>每秒一格：這一秒幾幀、最慢一幀、最大漂移、最大 |高差|。</summary>
        private struct Second
        {
            public int Frames;
            public float WorstDt;
            public float Drift;
            public float Gap;
            public float HeadSpeed;
        }

        private readonly Queue<Second> _window = new Queue<Second>();
        private Second _current;
        private float _secondStarted;
        private float _lastLog;
        private int _lastFrames;

        private readonly List<ARRaycastHit> _hits = new List<ARRaycastHit>();
        private readonly List<XRDisplaySubsystem> _displays = new List<XRDisplaySubsystem>();

        public static MrProbe Create(Camera camera, ARRaycastManager raycasts)
        {
            var go = new GameObject("MR Probe");
            var probe = go.AddComponent<MrProbe>();
            probe._camera = camera;
            probe._raycasts = raycasts;

            probe._text = go.AddComponent<TextMeshPro>();
            probe._text.font = NextFonts.Ui;
            probe._text.fontSize = 0.22f;                         // 3D 文字：約 2 cm 高的字（1.1 m 外）
            probe._text.alignment = TextAlignmentOptions.TopLeft;
            probe._text.textWrappingMode = TextWrappingModes.Normal;
            probe._text.rectTransform.sizeDelta = new Vector2(0.62f, 0.2f);
            probe._text.color = Color.white;
            probe._text.outlineWidth = 0.18f;                    // passthrough 上什麼背景都有 ⇒ 描黑邊
            probe._text.outlineColor = new Color32(0, 0, 0, 220);
            return probe;
        }

        /// <summary>她站好了：從這一刻起量高差與漂移。</summary>
        public void Track(Stage3DRig rig, ARPlane surface, float surfaceY)
        {
            _rig = rig;
            _hasLastHead = false;
            _trackedAt = Time.unscaledTime;
            _surface = surface;
            _surfaceY = surfaceY;
            if (surface != null) _onPlane = surface.transform.InverseTransformPoint(rig.Root.transform.position);
        }

        private void LateUpdate()
        {
            var now = Time.unscaledTime;
            var dt = Time.unscaledDeltaTime;
            Measure();

            _current.Frames++;
            _current.WorstDt = Mathf.Max(_current.WorstDt, dt);
            if (!float.IsNaN(_drift)) _current.Drift = Mathf.Max(_current.Drift, _drift);
            if (!float.IsNaN(_gap)) _current.Gap = Mathf.Max(_current.Gap, Mathf.Abs(_gap));
            if (!float.IsNaN(_headSpeed)) _current.HeadSpeed = Mathf.Max(_current.HeadSpeed, _headSpeed);
            if (now - _secondStarted >= 1f)
            {
                _lastFrames = _current.Frames;
                _window.Enqueue(_current);
                while (_window.Count > WindowSeconds) _window.Dequeue();
                _current = default;
                _secondStarted = now;
                _hz = RefreshRate();
            }

            if (_text.enabled != Visible) _text.enabled = Visible;
            if (Visible) Follow(dt);
            var shown = Compose(out var line);
            if (Visible) _text.text = shown;
            if (now - _lastLog >= LogEvery && _window.Count > 0)
            {
                _lastLog = now;
                Debug.Log("[mr] probe " + line);
            }
        }

        private void Measure()
        {
            if (_rig == null || _rig.Root == null) return;
            var root = _rig.Root.transform.position;
            var sole = _rig.SoleY;

            var surface = _surface != null ? _surface.transform.position.y : _surfaceY;
            var ray = new Ray(new Vector3(root.x, sole + 0.3f, root.z), Vector3.down);
            if (_raycasts != null && _raycasts.Raycast(ray, _hits, TrackableType.PlaneWithinPolygon))
            {
                // 最近的那一片（腳底下就是她站的那一面）
                surface = _hits[0].pose.position.y;
            }
            _gap = sole - surface;

            _drift = _surface != null ? Vector3.Distance(_surface.transform.TransformPoint(_onPlane), root) : float.NaN;

            // 頭相對身體（Root）的轉動速度：身體整個轉身不算抖，只看頸／頭自己甩得多快
            var head = _rig.HeadBone;
            var dt = Time.unscaledDeltaTime;
            if (head != null && dt > 1e-4f)
            {
                var local = Quaternion.Inverse(_rig.Root.transform.rotation) * head.rotation;
                _headSpeed = _hasLastHead && Time.unscaledTime - _trackedAt > HeadSpeedSettle
                    ? Quaternion.Angle(_lastHead, local) / dt : float.NaN;
                _lastHead = local;
                _hasLastHead = true;
            }
        }

        private float RefreshRate()
        {
            SubsystemManager.GetSubsystems(_displays);
            foreach (var display in _displays)
            {
                if (display.running && display.TryGetDisplayRefreshRate(out var rate)) return rate;
            }
            return float.NaN;
        }

        /// <summary>跟著頭、慢半拍（不鎖死在臉上，轉頭時看得到它滑過來），永遠面向你。</summary>
        private void Follow(float dt)
        {
            if (_camera == null) return;
            var head = _camera.transform;
            var target = head.position + head.forward * Distance + head.up * Rise;
            var t = 1f - Mathf.Exp(-3f * dt);
            transform.position = Vector3.Lerp(transform.position, target, t);
            transform.rotation = Quaternion.LookRotation(transform.position - head.position, Vector3.up);
        }

        /// <summary>畫面上的字（i18n）＋同一組數字的 logcat 行（固定英數，方便 grep）。</summary>
        private string Compose(out string line)
        {
            int frames = 0, seconds = 0;
            float worst = 0f, drift = 0f, gap = 0f, headSpeed = 0f;
            foreach (var second in _window)
            {
                frames += second.Frames;
                seconds++;
                worst = Mathf.Max(worst, second.WorstDt);
                drift = Mathf.Max(drift, second.Drift);
                gap = Mathf.Max(gap, second.Gap);
                headSpeed = Mathf.Max(headSpeed, second.HeadSpeed);
            }
            var fps60 = seconds > 0 ? (float)frames / seconds : float.NaN;
            var placed = _rig != null;

            line = $"gap={Cm(_gap)}cm gapMax60={(placed ? Cm(gap) : "-")}cm drift={Cm(_drift)}cm "
                   + $"driftMax60={(placed && _surface != null ? Cm(drift) : "-")}cm fps1={_lastFrames} "
                   + $"fps60={Num(fps60, "F1")} worst60={Num(worst * 1000f, "F1")}ms hz={Num(_hz, "F0")} "
                   + $"surface={Surface(placed)} "
                   + $"occ={Occ()} "
                   + $"bearing={Num(Bearing, "F0")} lookYaw={(placed ? Num(_rig.LookYaw, "F0") : "-")} "
                   + $"lookPitch={(placed ? Num(_rig.LookPitch, "F0") : "-")} headSpeedMax60={(placed ? Num(headSpeed, "F0") : "-")}";

            var text = string.IsNullOrEmpty(StatusKey) ? "" : NextStrings.Get(StatusKey) + "\n";
            if (!string.IsNullOrEmpty(Conversation)) text += Conversation + "\n";
            if (!string.IsNullOrEmpty(Note)) text += Note + "\n";
            if (placed)
            {
                text += NextStrings.F("mr_hud_ground", "gap", Cm(_gap), "max", Cm(gap)) + "\n";
                text += _surface != null
                    ? NextStrings.F("mr_hud_drift", "drift", Cm(drift)) + "\n"
                    : NextStrings.Get("mr_hud_drift_none") + "\n";
            }
            text += NextStrings.F("mr_hud_fps", "fps", _lastFrames, "avg", Num(fps60, "F1"),
                                  "worst", Num(worst * 1000f, "F1"), "hz", Num(_hz, "F0"));
            if (placed)
                text += "\n" + NextStrings.F("mr_hud_look", "bearing", Num(Bearing, "F0"),
                                             "yaw", Num(_rig.LookYaw, "F0"), "pitch", Num(_rig.LookPitch, "F0"));
            if (!string.IsNullOrEmpty(OcclusionKey)) text += "\n" + NextStrings.Get(OcclusionKey);
            text += "\n" + NextStrings.Get("mr_hud_keys");
            return text;
        }

        /// <summary>logcat 用：她站在哪一種面上（table／floor／plane＝沒分類的平面／estimated＝推定地面）。</summary>
        private string Surface(bool placed) =>
            !placed ? "-"
            : _surface == null ? "estimated"
            : (_surface.classifications & PlaneClassifications.Table) != 0 ? "table"
            : (_surface.classifications & PlaneClassifications.Floor) != 0 ? "floor" : "plane";

        /// <summary>logcat 用的遮擋狀態（固定英文，方便 grep 比幀率）。</summary>
        private string Occ() =>
            OcclusionKey == "mr_hud_occlusion_on" ? "on"
            : OcclusionKey == "mr_hud_occlusion_off" ? "off"
            : OcclusionKey == "mr_hud_occlusion_wait" ? "wait" : "-";

        private static string Cm(float meters) => Num(meters * 100f, "F1");

        private static string Num(float value, string format) =>
            float.IsNaN(value) ? "-" : value.ToString(format, CultureInfo.InvariantCulture);
    }
}
