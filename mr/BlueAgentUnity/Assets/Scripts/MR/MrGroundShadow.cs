using BlueAgent.Next;
using UnityEngine;

namespace BlueAgent.MR
{
    /// <summary>
    /// 她腳下的接地陰影（PLAN-mr-quest3 M1）：一片貼地的橢圓，跟著她的腳與朝向走，高度釘在量到的地面上。
    /// 著色見 `Shaders/MrGroundShadow.shader`（柔邊、可被真實家具遮住）。
    /// </summary>
    public sealed class MrGroundShadow : MonoBehaviour
    {
        public const string ShaderName = "BlueAgent/MrGroundShadow";

        /// <summary>陰影直徑佔身高的比例（左右／前後；Q 版肩寬、裙擺，前後比左右窄）。</summary>
        private const float Width = 0.40f;
        private const float Depth = 0.30f;
        /// <summary>浮在地面上方一點，免得跟地面平面的深度打架閃爍。</summary>
        private const float Lift = 0.003f;

        private Material _material;

        /// <summary>造一片；shader 找不到（沒釘進 Always Included）就回 null，MR 照跑、只是沒陰影。</summary>
        public static MrGroundShadow Create()
        {
            var shader = Shader.Find(ShaderName);
            if (shader == null)
            {
                Debug.LogWarning($"[mr] 找不到 {ShaderName}（沒釘進 Always Included？）⇒ 沒有接地陰影");
                return null;
            }
            var go = GameObject.CreatePrimitive(PrimitiveType.Quad);
            go.name = "MR Ground Shadow";
            Destroy(go.GetComponent<Collider>());
            var shadow = go.AddComponent<MrGroundShadow>();
            shadow._material = new Material(shader) { name = "MrGroundShadow" };
            var renderer = go.GetComponent<MeshRenderer>();
            renderer.sharedMaterial = shadow._material;
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;
            go.SetActive(false);
            return shadow;
        }

        private static readonly int StrengthId = Shader.PropertyToID("_Strength");
        /// <summary>陰影濃度（跟 shader 的預設同一個數）；淡入淡出時乘上去。</summary>
        private const float Strength = 0.45f;

        /// <summary>每幀（她動完之後）：放到她腳下、地面高度、跟她同一個朝向；<paramref name="fade"/>＝跟她一起淡入淡出。</summary>
        public void Follow(Stage3DRig rig, float floorY, float fade = 1f)
        {
            if (_material != null) _material.SetFloat(StrengthId, Strength * Mathf.Clamp01(fade));
            if (rig == null || rig.Root == null)
            {
                gameObject.SetActive(false);
                return;
            }
            var root = rig.Root.transform;
            var height = rig.Bounds.size.y;
            // 她的「前」是載入時量的（腳跟→腳尖），之後轉身都轉在 Root 上
            var forward = Vector3.ProjectOnPlane(root.rotation * rig.Forward, Vector3.up);
            if (forward.sqrMagnitude < 1e-6f) forward = Vector3.forward;
            transform.position = new Vector3(root.position.x, floorY + Lift, root.position.z);
            // Quad 預設面向 -Z ⇒ 先躺平（朝上），再轉到她的朝向
            transform.rotation = Quaternion.LookRotation(forward, Vector3.up) * Quaternion.Euler(90f, 0f, 0f);
            transform.localScale = new Vector3(height * Width, height * Depth, 1f);
            gameObject.SetActive(true);
        }

        private void OnDestroy()
        {
            if (_material != null) Destroy(_material);
        }
    }
}
