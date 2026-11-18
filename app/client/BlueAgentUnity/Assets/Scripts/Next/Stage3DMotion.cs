using System;
using System.Collections.Generic;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 一段 `bamotion/1` 動作片段（後端 Kimodo 生成；規格 `docs/PLAN-motion-kimodo-20261031.md` §4）。
    ///
    /// 片段裡是 SOMA 座標（右手、Y 上、+Z 前）：每個關節的**世界旋轉、相對 T-pose**，以及 Hips 的位置。
    /// 這裡解開時就換成 Unity 座標（左手）：鏡射 X ⇒ 位置 (−x, y, z)、四元數 (x, −y, −z, w)。
    /// 角色的左右不用換名字：SOMA 角色的左邊在 +X，鏡射後在 −X，正好是 Unity 裡「面向 +Z 的角色的左邊」。
    /// </summary>
    public sealed class MotionClip
    {
        public const string Format = "bamotion/1";

        public string Id { get; private set; }
        public string Tag { get; private set; }
        public int Fps { get; private set; }
        public int Frames { get; private set; }
        public float Seconds => Frames / (float)Mathf.Max(1, Fps);
        public float HipsRestHeight { get; private set; }

        private readonly Dictionary<string, int> _joints = new Dictionary<string, int>();
        private Quaternion[] _rotations;     // [frame * joints + joint]
        private Vector3[] _hips;
        private int _jointCount;

        public static MotionClip Parse(string text)
        {
            var json = new JObject(text);
            if (json.OptString("format") != Format)
                throw new FormatException("not a " + Format + " clip: " + json.OptString("format"));
            var clip = new MotionClip
            {
                Id = json.OptString("id"),
                Tag = json.OptString("tag"),
                Fps = json.OptInt("fps", 30),
                Frames = json.OptInt("frames", 0),
                HipsRestHeight = (float)json.OptDouble("hipsRestHeight", 1.0),
            };
            var names = json.OptJArray("joints");
            for (var i = 0; names != null && i < names.Length(); i++) clip._joints[names.OptString(i)] = i;
            clip._jointCount = clip._joints.Count;
            if (clip.Frames <= 0 || clip._jointCount == 0 || clip.Fps <= 0)
                throw new FormatException("empty clip");

            var rotations = Floats(json.OptString("rotations"), clip.Frames * clip._jointCount * 4, "rotations");
            clip._rotations = new Quaternion[clip.Frames * clip._jointCount];
            for (var i = 0; i < clip._rotations.Length; i++)
            {
                // 右手 → 左手（鏡射 X）
                clip._rotations[i] = new Quaternion(rotations[i * 4], -rotations[i * 4 + 1], -rotations[i * 4 + 2],
                                                    rotations[i * 4 + 3]).normalized;
            }
            var hips = Floats(json.OptString("hips"), clip.Frames * 3, "hips");
            clip._hips = new Vector3[clip.Frames];
            for (var f = 0; f < clip.Frames; f++) clip._hips[f] = new Vector3(-hips[f * 3], hips[f * 3 + 1], hips[f * 3 + 2]);
            return clip;
        }

        private static float[] Floats(string base64, int expected, string field)
        {
            var bytes = Convert.FromBase64String(base64 ?? "");
            if (bytes.Length != expected * 4)
                throw new FormatException($"{field}: {bytes.Length} bytes, expected {expected * 4}");
            var values = new float[expected];
            Buffer.BlockCopy(bytes, 0, values, 0, bytes.Length);     // float32 小端（我們的平台都是小端）
            return values;
        }

        public bool Has(string joint) => _joints.ContainsKey(joint);

        /// <summary>某個關節在時間 t 的世界旋轉差量（相對 T-pose，Unity 座標）。</summary>
        public Quaternion Rotation(string joint, float time)
        {
            if (!_joints.TryGetValue(joint, out var j)) return Quaternion.identity;
            Locate(time, out var a, out var b, out var k);
            return Quaternion.Slerp(_rotations[a * _jointCount + j], _rotations[b * _jointCount + j], k);
        }

        public Vector3 Hips(float time)
        {
            Locate(time, out var a, out var b, out var k);
            return Vector3.Lerp(_hips[a], _hips[b], k);
        }

        public Vector3 FirstHips => _hips[0];

        private void Locate(float time, out int a, out int b, out float k)
        {
            var position = Mathf.Clamp(time * Fps, 0f, Frames - 1);
            a = Mathf.FloorToInt(position);
            b = Mathf.Min(a + 1, Frames - 1);
            k = position - a;
        }
    }

    /// <summary>
    /// 把 <see cref="MotionClip"/>（SOMA 骨架）**重定向**到 BA 的 Bip001 骨架上播（owner 2026-10-31：Kimodo 驅動模型）。
    ///
    /// 做法＝世界旋轉差量：兩邊的靜止姿勢都是 T-pose ⇒ 每根骨頭的目標＝角色朝向 · D(t) · 角色朝向⁻¹ · BA 自己的 T-pose 世界旋轉。
    /// 跟骨頭本地軸怎麼擺無關，所以三個角色同一套。SOMA 多出來的 Spine2、Neck2 自然被吸收（BA 只有 Spine／Spine1／Neck）。
    ///
    /// ⚠️ BA 的 FBX 左右鏡像過（`Bip001 L …` 可能在角色的右邊）⇒ 左右**照身體實際的位置**對應，不照名字。
    /// Hips 位移按「BA 胯高 ／ SOMA 胯高」縮放；水平位移夾小（舞台很窄、Q 版也不該走出畫面）。
    /// 播放時淡入淡出、跟程序化待機混合；頭髮／袖子物理在這之後才算，所以會被動作帶著甩。
    /// </summary>
    public sealed class Stage3DMotion
    {
        private const float FadeIn = 0.25f;
        private const float FadeOut = 0.35f;
        /// <summary>水平位移上限（BA 尺度，m）。</summary>
        private const float MaxShift = 0.08f;

        private sealed class Link
        {
            public string Joint;
            public Transform Bone;
            public Quaternion RestFromRoot;   // T-pose 時：Inverse(root.rotation) * bone.rotation
        }

        /// <summary>SOMA 關節 → BA 骨頭（`{S}`＝角色那一側的 L／R；父骨在前）。</summary>
        private static readonly string[,] Map =
        {
            { "Hips", "Bip001 Pelvis" },
            { "Spine1", "Bip001 Spine" },
            { "Chest", "Bip001 Spine1" },
            { "Neck1", "Bip001 Neck" },
            { "Head", "Bip001 Head" },
            { "LeftShoulder", "Bip001 {L} Clavicle" },
            { "LeftArm", "Bip001 {L} UpperArm" },
            { "LeftForeArm", "Bip001 {L} Forearm" },
            { "LeftHand", "Bip001 {L} Hand" },
            { "RightShoulder", "Bip001 {R} Clavicle" },
            { "RightArm", "Bip001 {R} UpperArm" },
            { "RightForeArm", "Bip001 {R} Forearm" },
            { "RightHand", "Bip001 {R} Hand" },
            { "LeftLeg", "Bip001 {L} Thigh" },
            { "LeftShin", "Bip001 {L} Calf" },
            { "LeftFoot", "Bip001 {L} Foot" },
            { "LeftToeBase", "Bip001 {L} Toe0" },
            { "RightLeg", "Bip001 {R} Thigh" },
            { "RightShin", "Bip001 {R} Calf" },
            { "RightFoot", "Bip001 {R} Foot" },
            { "RightToeBase", "Bip001 {R} Toe0" },
        };

        private readonly List<Link> _links = new List<Link>();
        private Transform _root;
        private Quaternion _facing;          // Root 座標裡：+Z → 角色正面
        private Transform _hipsBone;         // Bip001（Pelvis 的父骨）：位移掛在它上面
        private Vector3 _hipsRestLocal;
        private float _hipsScale = 0.5f;

        private MotionClip _clip;
        private float _time;

        public bool Playing => _clip != null;
        public int LinkCount => _links.Count;
        /// <summary>這些骨頭會被動作寫入 ⇒ 待機每幀要把它們放回靜止姿勢（見 `Stage3DRig.Animate`）。</summary>
        public IEnumerable<Transform> Bones { get { foreach (var link in _links) yield return link.Bone; } }

        /// <summary>
        /// **在 T-pose 時建**（手臂放下之前）：記下每根骨頭的 T-pose 世界旋轉。
        /// <paramref name="forward"/>＝角色正面（世界）；<paramref name="right"/>＝角色的右邊（世界）。
        /// </summary>
        public static Stage3DMotion Build(Transform root, Transform model, Vector3 forward, Vector3 right,
                                          Func<Transform, string, Transform> find, float feetHeight)
        {
            var motion = new Stage3DMotion { _root = root };
            motion._facing = Quaternion.FromToRotation(Vector3.forward,
                                                       root.InverseTransformDirection(forward));

            // 哪一個字母（L／R）在角色的左邊：看上臂在身體哪一側（FBX 鏡像過，名字不可信）
            var upperL = find(model, "Bip001 L UpperArm");
            var pelvis = find(model, "Bip001 Pelvis");
            var center = pelvis != null ? pelvis.position : model.position;
            var lIsLeft = upperL == null || Vector3.Dot(upperL.position - center, right) < 0f;
            var left = lIsLeft ? "L" : "R";
            var rightLetter = lIsLeft ? "R" : "L";

            for (var i = 0; i < Map.GetLength(0); i++)
            {
                var name = Map[i, 1].Replace("{L}", left).Replace("{R}", rightLetter);
                var bone = find(model, name);
                if (bone == null) continue;
                motion._links.Add(new Link
                {
                    Joint = Map[i, 0],
                    Bone = bone,
                    RestFromRoot = Quaternion.Inverse(root.rotation) * bone.rotation,
                });
            }

            if (pelvis != null && pelvis.parent != null)
            {
                motion._hipsBone = pelvis.parent;
                motion._hipsRestLocal = motion._hipsBone.localPosition;
                motion._hipsScale = Mathf.Max(0.1f, pelvis.position.y - feetHeight);   // 先存 BA 胯高，播放時除以片段的
            }
            Debug.Log($"[next] 3D 動作：重定向 {motion._links.Count} 根骨頭（L 在角色{(lIsLeft ? "左" : "右")}邊）");
            return motion;
        }

        public void Play(MotionClip clip)
        {
            _clip = clip;
            _time = 0f;
        }

        public void Stop() => _clip = null;

        /// <summary>每幀：程序化姿勢擺好之後、物理之前叫。</summary>
        public void Apply(float dt)
        {
            if (_hipsBone != null) _hipsBone.localPosition = _hipsRestLocal;
            if (_clip == null) return;
            _time += dt;
            var duration = _clip.Seconds;
            if (_time >= duration)
            {
                _clip = null;
                return;
            }
            var weight = Smooth(_time / FadeIn) * Smooth((duration - _time) / FadeOut);

            var rootRotation = _root.rotation;
            var frame = rootRotation * _facing;
            var frameInverse = Quaternion.Inverse(frame);
            foreach (var link in _links)
            {
                if (!_clip.Has(link.Joint)) continue;
                var delta = _clip.Rotation(link.Joint, _time);
                var target = frame * delta * frameInverse * (rootRotation * link.RestFromRoot);
                link.Bone.rotation = Quaternion.Slerp(link.Bone.rotation, target, weight);
            }

            if (_hipsBone != null && _hipsBone.parent != null)
            {
                var scale = _hipsScale / Mathf.Max(0.1f, _clip.HipsRestHeight);
                var hips = _clip.Hips(_time);
                var first = _clip.FirstHips;
                var shift = new Vector3(
                    Mathf.Clamp((hips.x - first.x) * scale, -MaxShift, MaxShift),
                    Mathf.Clamp((hips.y - _clip.HipsRestHeight) * scale, -0.3f * _hipsScale, 0.15f * _hipsScale),
                    Mathf.Clamp((hips.z - first.z) * scale, -MaxShift, MaxShift));
                var world = frame * shift * weight;
                _hipsBone.localPosition = _hipsRestLocal + _hipsBone.parent.InverseTransformVector(world);
            }
        }

        private static float Smooth(float x)
        {
            x = Mathf.Clamp01(x);
            return x * x * (3f - 2f * x);
        }
    }
}
