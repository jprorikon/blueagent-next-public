// 3D 立繪的卡通著色（owner 2026-10-31：「現在渲染的模型缺乏陰影，按照 toon shader 的方式渲染」）。
//
// * 兩階明暗：亮面＝貼圖原色，暗面＝貼圖 × `_ShadeColor`（偏紫灰，二次元常見的陰影色），界線用 smoothstep 收一點軟邊。
// * 光不靠場景燈：方向由 `Stage3DRig.Light` 每幀照相機擺（`_Stage3DLightDir`，世界座標、指向光源），
//   UI 那台主相機與其他東西完全不受影響，換角度看也一樣是「左上前方打光」。
// * 雙面網格（裙子、頭髮內側）：法線**不**照正反面翻——FBX 轉檔把左右鏡像了，看得見的那一面在 GPU 眼裡是「背面」，
//   照背面翻反而整身變暗（2026-10-31 無頭預覽實測）。
// * 邊緣光：亮面那一側沿輪廓一圈淡淡的白。
// * 描邊：反轉外殼（第二個 pass，`SRPDefaultUnlit`，URP 會在主 pass 之外再畫它）——沿法線往外推、只畫背面、
//   顏色＝貼圖 × `_OutlineColor`；深度往後挪一點，單片網格（頭髮片、裙擺）從背面看時主 pass 會蓋過它，不會整片變黑。
//   `_OutlineWidth` 0 ＝這個材質不描（臉、眼、嘴、光環）。
// * 裁切用 `_Cutoff`（嘴型圖集那一層），**不走 shader keyword** ⇒ 沒有變體被建置裁掉的風險。
// * 立體（MR／VR，2026-10-31）：Quest 走 OpenXR 的單趟立體（multiview／instanced）⇒ 每個 pass 都要帶 stereo 巨集，
//   不然只有左眼畫得到她。不在 XR 時這些巨集展開成空的，手機那條路不受影響。
// * 遮擋（MR M1，2026-10-31）：`MrOcclusion.hlsl`——真實物體在她前面時 alpha 降成 0、顏色預乘，passthrough 透出來。
//   只有 `XR_HARD_OCCLUSION`／`XR_SOFT_OCCLUSION` 全域關鍵字開著（MR 的 ARShaderOcclusion 設）才有作用；手機上恆為 1。
// * 淡入淡出（MR M4，2026-11-01）：`_Fade` 乘在同一個可見度上（召喚時從 passthrough 裡浮現）。手機上恆為 1。
// 執行期 `Shader.Find`，所以 `HeadlessBuild.EnsureAlwaysIncludedShaders` 要把它釘進 Always Included。
Shader "BlueAgent/Stage3DToon"
{
    Properties
    {
        _BaseMap ("Base Map", 2D) = "white" {}
        _BaseColor ("Base Color", Color) = (1, 1, 1, 1)
        _Cutoff ("Alpha Cutoff (<0 = off)", Range(-0.01, 1)) = -0.01
        _ShadeColor ("Shade Color", Color) = (0.70, 0.62, 0.78, 1)
        _ShadeStrength ("Shade Strength", Range(0, 1)) = 1
        _ShadeThreshold ("Shade Threshold", Range(-1, 1)) = 0.05
        _ShadeSoftness ("Shade Softness", Range(0.001, 0.5)) = 0.04
        _RimColor ("Rim Color", Color) = (1, 1, 1, 1)
        _RimStrength ("Rim Strength", Range(0, 1)) = 0.2
        _OutlineWidth ("Outline Width (m)", Float) = 0.0025
        _OutlineColor ("Outline Color", Color) = (0.32, 0.26, 0.32, 1)
        _Cull ("Cull", Float) = 0
        _Fade ("Fade (MR summon)", Range(0, 1)) = 1
    }

    SubShader
    {
        Tags { "RenderType" = "Opaque" "RenderPipeline" = "UniversalPipeline" "Queue" = "Geometry" }

        HLSLINCLUDE
        #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"
        #include "MrOcclusion.hlsl"

        TEXTURE2D(_BaseMap);
        SAMPLER(sampler_BaseMap);

        CBUFFER_START(UnityPerMaterial)
            float4 _BaseMap_ST;
            half4 _BaseColor;
            half _Cutoff;
            half4 _ShadeColor;
            half _ShadeStrength;
            half _ShadeThreshold;
            half _ShadeSoftness;
            half4 _RimColor;
            half _RimStrength;
            float _OutlineWidth;
            half4 _OutlineColor;
            half _Cull;
            half _Fade;
        CBUFFER_END

        float4 _Stage3DLightDir;

        struct Attributes
        {
            float4 positionOS : POSITION;
            float3 normalOS : NORMAL;
            float2 uv : TEXCOORD0;
            UNITY_VERTEX_INPUT_INSTANCE_ID
        };
        ENDHLSL

        Pass
        {
            Name "Toon"
            Tags { "LightMode" = "UniversalForward" }
            Cull [_Cull]

            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma multi_compile_instancing
            #pragma multi_compile_fragment _ XR_HARD_OCCLUSION XR_SOFT_OCCLUSION

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                float2 uv : TEXCOORD0;
                float3 normalWS : TEXCOORD1;
                float3 positionWS : TEXCOORD2;
                UNITY_VERTEX_OUTPUT_STEREO
            };

            Varyings vert(Attributes input)
            {
                Varyings output;
                UNITY_SETUP_INSTANCE_ID(input);
                UNITY_INITIALIZE_VERTEX_OUTPUT_STEREO(output);
                VertexPositionInputs position = GetVertexPositionInputs(input.positionOS.xyz);
                output.positionCS = position.positionCS;
                output.positionWS = position.positionWS;
                output.normalWS = TransformObjectToWorldNormal(input.normalOS);
                output.uv = TRANSFORM_TEX(input.uv, _BaseMap);
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                UNITY_SETUP_STEREO_EYE_INDEX_POST_VERTEX(input);   // 視線方向要用這隻眼的相機位置
                half4 color = SAMPLE_TEXTURE2D(_BaseMap, sampler_BaseMap, input.uv) * _BaseColor;
                clip(color.a - _Cutoff);

                float3 normal = normalize(input.normalWS);
                float3 light = normalize(_Stage3DLightDir.xyz + float3(0.0, 1e-5, 0.0));
                half lit = smoothstep(_ShadeThreshold - _ShadeSoftness, _ShadeThreshold + _ShadeSoftness,
                                      dot(normal, light));
                half3 shade = lerp(half3(1.0, 1.0, 1.0), _ShadeColor.rgb, _ShadeStrength);
                half3 rgb = color.rgb * lerp(shade, half3(1.0, 1.0, 1.0), lit);

                float3 view = normalize(GetWorldSpaceViewDir(input.positionWS));
                half rim = pow(saturate(1.0 - abs(dot(normal, view))), 4.0) * lit * _RimStrength;
                rgb += _RimColor.rgb * rim;
                half visible = MrVisibility(input.positionWS) * _Fade;
                return half4(rgb * visible, visible);
            }
            ENDHLSL
        }

        Pass
        {
            Name "Outline"
            Tags { "LightMode" = "SRPDefaultUnlit" }
            Cull Front

            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma multi_compile_instancing
            #pragma multi_compile_fragment _ XR_HARD_OCCLUSION XR_SOFT_OCCLUSION

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                float2 uv : TEXCOORD0;
                float3 positionWS : TEXCOORD1;
                UNITY_VERTEX_OUTPUT_STEREO
            };

            Varyings vert(Attributes input)
            {
                Varyings output;
                UNITY_SETUP_INSTANCE_ID(input);
                UNITY_INITIALIZE_VERTEX_OUTPUT_STEREO(output);
                output.uv = TRANSFORM_TEX(input.uv, _BaseMap);
                output.positionWS = float3(0.0, 0.0, 0.0);
                if (_OutlineWidth <= 0.0)
                {
                    output.positionCS = float4(0.0, 0.0, 0.0, 1.0);   // 全部縮成一點 ⇒ 不畫
                    return output;
                }
                float3 positionWS = TransformObjectToWorld(input.positionOS.xyz);
                float3 normalWS = normalize(TransformObjectToWorldNormal(input.normalOS));
                output.positionWS = positionWS + normalWS * _OutlineWidth;
                output.positionCS = TransformWorldToHClip(output.positionWS);
                // 深度往後挪一點：單片網格從背面看時，主 pass 贏
                #if UNITY_REVERSED_Z
                    output.positionCS.z -= 0.0004 * output.positionCS.w;
                #else
                    output.positionCS.z += 0.0004 * output.positionCS.w;
                #endif
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                UNITY_SETUP_STEREO_EYE_INDEX_POST_VERTEX(input);
                half4 color = SAMPLE_TEXTURE2D(_BaseMap, sampler_BaseMap, input.uv) * _BaseColor;
                clip(color.a - _Cutoff);
                half visible = MrVisibility(input.positionWS) * _Fade;
                return half4(color.rgb * _OutlineColor.rgb * visible, visible);
            }
            ENDHLSL
        }
    }
}
