// 手機端的 MR 遮擋替身：`Stage3DToon.shader` 是手機與 Quest 共用的著色器，固定 `#include "MrOcclusion.hlsl"`。
//
// 為什麼放在這裡（2026-11-07）：S4-n 把 Quest 專案拆到頂層 `mr/` 時，真正的遮擋實作
// （`mr/BlueAgentUnity/Assets/Shaders/MrOcclusion.hlsl`，要 AR Foundation 的 Utils.hlsl）跟著搬走了，
// 手機工程從此找不到這個 include ⇒ Unity 出包報 `Couldn't open include file`。
// 手機沒有 passthrough、也不裝 AR Foundation ⇒ 不論 `XR_*_OCCLUSION` 關鍵字開不開，可見度一律 1。
//
// MR 構建時 `mr/tools/sync_workspace.py` 先複製 app/client 的 Assets，再用 mr/ 的 Assets **覆蓋**，
// 這個檔會被真正的實作取代 ⇒ Quest 的遮擋行為不受影響。介面（函式名與簽名）必須和那一份一致。
#ifndef BLUEAGENT_MR_OCCLUSION_INCLUDED
#define BLUEAGENT_MR_OCCLUSION_INCLUDED

half MrVisibility(float3 positionWS) { return 1.0; }

#endif
