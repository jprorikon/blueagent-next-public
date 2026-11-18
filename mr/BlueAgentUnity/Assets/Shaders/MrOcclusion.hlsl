// MR（Quest 3）的環境深度遮擋：真實的桌子、沙發在她前面時，把她那幾個像素「讓給」passthrough。
//
// 資料來源：`MrShell` 掛的 AROcclusionManager ＋ ARShaderOcclusion 每幀設的全域值
//   `_EnvironmentDepthTexture`（Meta 的深度圖，每隻眼一層）、`_EnvironmentDepthProjectionMatrices`、
//   `_NdcLinearConversionParameters`（在 AR Foundation 的 Utils.hlsl）、`_IsOcclusionOn`，
//   與全域關鍵字 `XR_HARD_OCCLUSION`／`XR_SOFT_OCCLUSION`（軟遮擋另有一張預處理過的深度圖）。
// 演算法取自 AR Foundation Samples 的 `Assets/Shaders/Occlusion/OcclusionComputation.hlsl`（main，2026-10-31），
// 只留硬／軟兩種，拿掉沒用到的「容忍度」那支與線性深度（Meta 給的是非線性深度）。
//
// 怎麼用：片段著色器最後 `half v = MrVisibility(positionWS); return half4(rgb * v, v);`
// ——passthrough 依 alpha 合成，alpha 0 的地方透出現實；顏色要預乘（官方範例同一個寫法）。
// 手機包、或 MR 裡遮擋沒開：兩個關鍵字都不在 ⇒ 整支展開成 `return 1`，一行成本都沒有。
#ifndef BLUEAGENT_MR_OCCLUSION_INCLUDED
#define BLUEAGENT_MR_OCCLUSION_INCLUDED

#if defined(XR_HARD_OCCLUSION) || defined(XR_SOFT_OCCLUSION)

#include "Packages/com.unity.xr.arfoundation/Assets/Shaders/Utils.hlsl"

TEXTURE2D_ARRAY_FLOAT(_EnvironmentDepthTexture);
SAMPLER(sampler_EnvironmentDepthTexture);
TEXTURE2D_ARRAY_FLOAT(_EnvironmentDepthTexturePreprocessed);
SAMPLER(sampler_EnvironmentDepthTexturePreprocessed);
float4x4 _EnvironmentDepthProjectionMatrices[2];
int _IsOcclusionOn;

// unity_StereoEyeIndex 只在立體（instanced／multiview）時才有定義
#if defined(UNITY_STEREO_INSTANCING_ENABLED) || defined(UNITY_STEREO_MULTIVIEW_ENABLED)
    #define MR_EYE unity_StereoEyeIndex
#else
    #define MR_EYE 0
#endif

float MrHardVisibility(float2 uv, float linearSceneDepth)
{
    float environment = SAMPLE_TEXTURE2D_ARRAY(_EnvironmentDepthTexture, sampler_EnvironmentDepthTexture, uv, MR_EYE).r;
    return float(LinearizeDepth(ConvertDepthToSymmetricRange(environment)) > linearSceneDepth);
}

float MrSoftVisibility(float2 uv, float linearSceneDepth)
{
    float4 depthSample = _EnvironmentDepthTexturePreprocessed.Sample(sampler_EnvironmentDepthTexturePreprocessed,
                                                                     float3(uv, MR_EYE));
    float sceneNdc = ConvertDepthToNonSymmetricRange(LinearDepthToSymmetricRangeNDC(linearSceneDepth));
    // 這些常數都是官方範例實驗出來的值
    float invScale = -15.0 / (sceneNdc - 1.0);
    float3 minMaxMid = float3(1.0 - depthSample.x, 1.0 - depthSample.y, depthSample.z + 1.0 - depthSample.x);
    float3 deltas = saturate((minMaxMid - sceneNdc) * invScale);
    float blend = smoothstep(0.1, 0.9, depthSample.z / depthSample.w);
    float isBlending = step(0.05, deltas.y - deltas.x);
    return deltas.z * (1.0 - isBlending) + lerp(deltas.x, deltas.y, blend) * isBlending;
}

half MrVisibility(float3 positionWS)
{
    if (_IsOcclusionOn == 0) return 1.0;
    float4 clip = mul(_EnvironmentDepthProjectionMatrices[MR_EYE], float4(positionWS, 1.0));
    float2 uv = (clip.xy / clip.w + 1.0) * 0.5;
    if (any(uv < 0.0) || any(uv > 1.0)) return 1.0;          // 深度圖拍不到的地方不遮
    float linearSceneDepth = LinearizeDepth(clip.z / clip.w);
    #if defined(XR_SOFT_OCCLUSION)
        return (half)MrSoftVisibility(uv, linearSceneDepth);
    #else
        return (half)MrHardVisibility(uv, linearSceneDepth);
    #endif
}

#else

half MrVisibility(float3 positionWS) { return 1.0; }

#endif
#endif
