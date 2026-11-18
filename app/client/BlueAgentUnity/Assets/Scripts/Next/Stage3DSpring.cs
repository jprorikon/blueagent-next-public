using System;
using System.Collections.Generic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 3D 立繪的**頭髮／袖子物理**（owner 2026-10-31：「頭髮和衣服大袖口能不能加點物理演算」）。
    ///
    /// 做法＝UniVRM SpringBone 那一套（每根骨頭一個「尾端點」做 Verlet）：慣性（帶阻尼）＋往靜止姿勢拉回的彈力＋重力，
    /// 再把尾端拉回骨長、推出碰撞球，最後把骨頭轉向尾端。不用 Unity 的物理引擎（不必掛 Rigidbody／Joint）。
    ///
    /// 哪些骨頭會動：BA 的模型沒有標記，**照名字**找鏈（`bone_hair*`、袖子 `yukata`／`Sode`、髮帶 `Ribbon`／`ribborn`），
    /// 一條鏈從「名字對得上、父骨對不上」的那一根開始。眼睛、眉毛、髮飾的花不動。
    /// 碰撞球：頭、胸、腰、骨盆、兩條大腿；大小從「主要由那根骨頭帶動的頂點」量出來，所以三個角色各自合身。
    ///
    /// 力的大小照骨長換算（VRM 的參數是按 ~10 cm 的骨頭調的，BA 的 Q 版骨頭只有幾公分，直接用會甩得太兇）。
    /// </summary>
    public sealed class Stage3DSpring
    {
        private sealed class Group
        {
            public string Name;
            public string[] Patterns;
            public float Stiffness;
            public float Gravity;
            public float Drag;
        }

        private static readonly Group[] Groups =
        {
            new Group { Name = "hair", Patterns = new[] { "bone_hair" }, Stiffness = 1.8f, Gravity = 0.1f, Drag = 0.6f },
            new Group { Name = "sleeve", Patterns = new[] { "yukata", "sode" }, Stiffness = 0.7f, Gravity = 0.35f, Drag = 0.45f },
            new Group { Name = "ribbon", Patterns = new[] { "ribbon", "ribborn" }, Stiffness = 0.9f, Gravity = 0.25f, Drag = 0.45f },
        };

        /// <summary>參數是照這個骨長調的（m）。</summary>
        private const float ReferenceLength = 0.1f;
        /// <summary>尾端點本身的半徑（m；碰撞時多推開這麼多）。</summary>
        private const float HitRadius = 0.008f;
        /// <summary>單步的 dt 上限：卡頓一下也不會一步甩飛。</summary>
        private const float MaxStep = 1f / 30f;
        /// <summary>
        /// 整個人轉身（拖曳）時，有多少比例的轉動算進頭髮／袖子的慣性。拖曳是「換角度看」不是她自己在甩頭，
        /// 全算的話快速一拖遠側的髮束會整片甩到胸前、直挺挺像刺（2026-10-31 無頭預覽：60° / 0.2 s）。
        /// </summary>
        private const float TurnInertia = 0.35f;
        /// <summary>每根骨頭離開靜止方向的最大角度（保險：再怎麼甩也不會折成一根刺）。</summary>
        private const float MaxAngle = 45f;

        private static readonly string[] ColliderBones =
            { "Bip001 Head", "Bip001 Spine1", "Bip001 Spine", "Bip001 Pelvis", "Bip001 L Thigh", "Bip001 R Thigh" };

        private sealed class Joint
        {
            public Transform Bone;
            public Group Group;
            public Quaternion LocalRest;
            public Vector3 Axis;          // 骨頭本地座標裡指向尾端的方向
            public float Length;
            public Vector3 Tail;
            public Vector3 PrevTail;
        }

        private sealed class Sphere
        {
            public Transform Bone;
            public Vector3 Offset;        // 骨頭本地座標
            public float Radius;
        }

        private readonly List<Joint> _joints = new List<Joint>();     // 父骨在前（逐鏈深度優先）
        private readonly List<Sphere> _spheres = new List<Sphere>();
        /// <summary>
        /// 模型的縮放（手機舞台＝1；MR 桌上 30 cm 的她約 0.26）。<see cref="HitRadius"/> 與「太小的球不要」的門檻
        /// 是公尺寫死的，要跟著縮，不然小的她頭髮會被一圈過厚的碰撞殼推開、小球也全被丟掉。
        /// </summary>
        private float _scale = 1f;
        private Quaternion _lastRoot = Quaternion.identity;
        private bool _hasLastRoot;

        public int JointCount => _joints.Count;
        public int ColliderCount => _spheres.Count;

        /// <summary>在手臂放下、靜止姿勢定好之後建（靜止姿勢就是那一刻的本地旋轉）。</summary>
        public static Stage3DSpring Build(Transform model, SkinnedMeshRenderer body)
        {
            var spring = new Stage3DSpring { _scale = Mathf.Max(1e-4f, model.lossyScale.x) };
            foreach (var node in model.GetComponentsInChildren<Transform>(true))
            {
                var group = GroupOf(node.name);
                if (group == null || node.parent == null || GroupOf(node.parent.name) != null) continue;
                spring.AddChain(node, group);
            }
            if (body != null) spring.BuildColliders(model, body);
            return spring;
        }

        private static Group GroupOf(string name)
        {
            var lower = name.ToLowerInvariant();
            foreach (var group in Groups)
            {
                foreach (var pattern in group.Patterns)
                {
                    if (lower.Contains(pattern)) return group;
                }
            }
            return null;
        }

        private void AddChain(Transform bone, Group group)
        {
            Transform child = null;
            foreach (Transform candidate in bone)
            {
                if (GroupOf(candidate.name) == group) { child = candidate; break; }
            }

            Vector3 tailWorld;
            if (child != null)
            {
                tailWorld = child.position;
            }
            else
            {
                // 鏈的最後一根沒有子骨：沿「父 → 自己」的方向延伸一小段當尾端（UniVRM 是固定 7 cm，Q 版縮小）
                var delta = bone.position - bone.parent.position;
                var extend = Mathf.Min(0.03f, delta.magnitude * 0.6f);
                tailWorld = bone.position + (delta.sqrMagnitude > 1e-10f ? delta.normalized : Vector3.down) * extend;
            }
            var length = Vector3.Distance(bone.position, tailWorld);
            if (length > 1e-4f)
            {
                _joints.Add(new Joint
                {
                    Bone = bone,
                    Group = group,
                    LocalRest = bone.localRotation,
                    Axis = bone.InverseTransformDirection(tailWorld - bone.position).normalized,
                    Length = length,
                    Tail = tailWorld,
                    PrevTail = tailWorld,
                });
            }
            if (child != null) AddChain(child, group);
            foreach (Transform other in bone)
            {
                if (other != child && GroupOf(other.name) == group) AddChain(other, group);   // 分岔的鏈
            }
        }

        /// <summary>碰撞球：主要由那根骨頭帶動（權重 &gt; 0.5）的頂點量 bounds，取水平兩軸較小的一半再縮一點當半徑。</summary>
        private void BuildColliders(Transform model, SkinnedMeshRenderer body)
        {
            var mesh = body.sharedMesh;
            if (mesh == null || !mesh.isReadable) return;
            var weights = mesh.boneWeights;
            if (weights == null || weights.Length == 0) return;
            var bones = body.bones;
            var baked = new Mesh();
            body.BakeMesh(baked, true);
            var vertices = baked.vertices;
            var toWorld = body.transform.localToWorldMatrix;
            foreach (var name in ColliderBones)
            {
                var bone = Array.Find(bones, b => b != null && b.name == name);
                if (bone == null) continue;
                var index = Array.IndexOf(bones, bone);
                var bounds = new Bounds();
                var count = 0;
                for (var v = 0; v < vertices.Length && v < weights.Length; v++)
                {
                    if (weights[v].boneIndex0 != index || weights[v].weight0 < 0.5f) continue;
                    var p = toWorld.MultiplyPoint3x4(vertices[v]);
                    if (count++ == 0) bounds = new Bounds(p, Vector3.zero);
                    else bounds.Encapsulate(p);
                }
                if (count < 10) continue;
                // 0.75：球太大會把本來就貼著臉／身體垂下的髮束推出去（2026-10-31 無頭預覽：臉旁翹出一根）
                var radius = 0.5f * Mathf.Min(bounds.size.x, bounds.size.z) * 0.75f;
                if (radius < 0.005f * _scale) continue;
                _spheres.Add(new Sphere { Bone = bone, Offset = bone.InverseTransformPoint(bounds.center), Radius = radius });
            }
            if (Application.isPlaying) UnityEngine.Object.Destroy(baked);
            else UnityEngine.Object.DestroyImmediate(baked);
        }

        /// <summary>走一步（主要骨頭的姿勢已經擺好之後叫；<paramref name="root"/>＝整個人轉身用的那一顆）。</summary>
        public void Step(float dt, Transform root)
        {
            if (dt <= 0f) return;
            dt = Mathf.Min(dt, MaxStep);
            CarryTurn(root);
            foreach (var joint in _joints)
            {
                var bone = joint.Bone;
                var origin = bone.position;
                var restRotation = bone.parent.rotation * joint.LocalRest;
                var restDirection = restRotation * joint.Axis;
                var scale = joint.Length / ReferenceLength;

                var next = joint.Tail
                           + (joint.Tail - joint.PrevTail) * (1f - joint.Group.Drag)
                           + restDirection * (joint.Group.Stiffness * dt * scale)
                           + Vector3.down * (joint.Group.Gravity * dt * scale);
                next = origin + (next - origin).normalized * joint.Length;

                foreach (var sphere in _spheres)
                {
                    var center = sphere.Bone.TransformPoint(sphere.Offset);
                    var reach = sphere.Radius + HitRadius * _scale;
                    var away = next - center;
                    if (away.sqrMagnitude >= reach * reach) continue;
                    next = center + (away.sqrMagnitude > 1e-10f ? away.normalized : restDirection) * reach;
                    next = origin + (next - origin).normalized * joint.Length;
                }

                var direction = next - origin;
                if (Vector3.Angle(restDirection, direction) > MaxAngle)
                {
                    direction = Vector3.RotateTowards(restDirection, direction, MaxAngle * Mathf.Deg2Rad, 0f);
                    next = origin + direction.normalized * joint.Length;
                }

                joint.PrevTail = joint.Tail;
                joint.Tail = next;
                bone.rotation = Quaternion.FromToRotation(restDirection, next - origin) * restRotation;
            }
        }

        /// <summary>
        /// 這一步整個人轉了多少：把 (1 − <see cref="TurnInertia"/>) 的轉動直接套到尾端點上（＝那一部分不產生慣性），
        /// 剩下的才讓彈簧去追。
        /// </summary>
        private void CarryTurn(Transform root)
        {
            if (root == null) return;
            var current = root.rotation;
            if (_hasLastRoot)
            {
                var delta = current * Quaternion.Inverse(_lastRoot);
                var carried = Quaternion.Slerp(Quaternion.identity, delta, 1f - TurnInertia);
                var pivot = root.position;
                foreach (var joint in _joints)
                {
                    joint.Tail = pivot + carried * (joint.Tail - pivot);
                    joint.PrevTail = pivot + carried * (joint.PrevTail - pivot);
                }
            }
            _lastRoot = current;
            _hasLastRoot = true;
        }

        /// <summary>整個人被瞬間搬動時（不是轉身）把尾端點跟過去，免得一口氣甩一大圈。</summary>
        public void Reset()
        {
            foreach (var joint in _joints)
            {
                joint.Bone.localRotation = joint.LocalRest;
                joint.Tail = joint.PrevTail = joint.Bone.position + joint.Bone.rotation * joint.Axis * joint.Length;
            }
        }
    }
}
