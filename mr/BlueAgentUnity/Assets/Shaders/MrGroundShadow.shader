// MR 的接地陰影（PLAN-mr-quest3 M1；§2-4：passthrough 沒有引擎的燈光與陰影，沒有它她像貼紙）。
//
// 一片貼在地面上的四邊形（`MrGroundShadow`），中心最暗、往外平滑淡掉的橢圓。不用即時陰影貼圖：
// 角色是程式化待機、腳幾乎不離地，一片柔和的接觸陰影就夠「站在這裡」，而且幾乎不花效能。
//
// passthrough 依 alpha 合成 ⇒ 顏色寫黑、alpha 寫陰影濃度（alpha 那一路用 One OneMinusSrcAlpha 累加，
// 不然背景 alpha 0 會把陰影的 alpha 吃掉、整片看不見）。前面有真實家具時跟她一樣被遮（MrOcclusion.hlsl）。
// 執行期 `Shader.Find` ⇒ `MrBuild` 建置時把它釘進 Always Included。
Shader "BlueAgent/MrGroundShadow"
{
    Properties
    {
        _Strength ("Strength", Range(0, 1)) = 0.45
        _Softness ("Softness", Range(0.05, 1)) = 0.55
    }

    SubShader
    {
        Tags { "RenderType" = "Transparent" "RenderPipeline" = "UniversalPipeline" "Queue" = "Transparent-10" }

        Pass
        {
            Name "GroundShadow"
            Tags { "LightMode" = "UniversalForward" }
            Blend SrcAlpha OneMinusSrcAlpha, One OneMinusSrcAlpha
            ZWrite Off
            Cull Off

            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma multi_compile_instancing
            #pragma multi_compile_fragment _ XR_HARD_OCCLUSION XR_SOFT_OCCLUSION
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"
            #include "MrOcclusion.hlsl"

            CBUFFER_START(UnityPerMaterial)
                half _Strength;
                half _Softness;
            CBUFFER_END

            struct Attributes
            {
                float4 positionOS : POSITION;
                float2 uv : TEXCOORD0;
                UNITY_VERTEX_INPUT_INSTANCE_ID
            };

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
                output.positionWS = TransformObjectToWorld(input.positionOS.xyz);
                output.positionCS = TransformWorldToHClip(output.positionWS);
                output.uv = input.uv;
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                UNITY_SETUP_STEREO_EYE_INDEX_POST_VERTEX(input);
                // uv 0–1 ⇒ 中心 0、邊緣 1 的距離；1 − softness 以內全黑，外圈平滑淡到 0
                float r = length(input.uv * 2.0 - 1.0);
                half shade = (1.0 - smoothstep(1.0 - _Softness, 1.0, r)) * _Strength;
                shade *= MrVisibility(input.positionWS);
                return half4(0.0, 0.0, 0.0, shade);
            }
            ENDHLSL
        }
    }
}
