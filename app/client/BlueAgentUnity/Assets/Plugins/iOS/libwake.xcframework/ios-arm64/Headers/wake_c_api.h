// A flat C face on `wake_core` — for callers that cannot speak C++.
//
// 為什麼要有這一支（2026-10-03）：iOS 要跑手機側喚醒網（owner：「和安卓一致」），而引擎本體是 C++
// （`wake_core.cpp`：log-mel ＋ DS-CNN ＋ 學生聲紋 ＋ 金標自檢）。**Kotlin/Native 的 cinterop 刻意
// 不支援 C++** ⇒ 兩端之間必須有一層 `extern "C"` 的薄殼。安卓走的是 JNI（`wake_jni.cpp`），
// 這一支給 iOS（以及其他講 C 的宿主）。
//
// 紀律：**一對一、不加狀態**。每個函式就是 `wake::Kws`／`wake::Spk` 的一個動作，唯一的狀態是
// 呼叫端手上那個 handle。任何「多算一點、少算一點」都會讓兩端的分數不再相同——金標自檢是唯一
// 的判準（`wake_c_api_selftest.cpp` 拿同一段 PCM 同時餵 C++ 與 C 兩條路，比對到一個位元）。
//
// 生命週期：`*_create` 回 handle，失敗回 NULL 並把原因寫進 `err`（呼叫端是手機，不能丟例外）；
// `*_destroy` 放掉。所有吃指標的函式都容許 NULL handle（回 0／NaN／甚麼都不做）。

#ifndef WAKE_C_API_H
#define WAKE_C_API_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct WKws WKws;
typedef struct WSpk WSpk;

/* ── 喚醒網（wake::Kws）────────────────────────────────────────────────────────────────────── */

/* blob 讀不進去回 NULL，並把訊息寫進 err（最多 err_cap-1 個位元組）。 */
WKws* wkws_create(const uint8_t* blob, int32_t len, char* err, int32_t err_cap);

/* blob 自帶的操作點。 */
float wkws_threshold(const WKws* k);

/* 每幾幀決一次（<1 視為 1）。 */
void wkws_set_decide_every(WKws* k, int32_t every);

/* 環形緩衝要留幾個樣本（閘門窗的跨距）。 */
void wkws_set_pcm_cap(WKws* k, int32_t samples);

/* `wkws_push(k, pcm, n, out, cap)` 最多會寫幾個分數 —— 呼叫端照這個開緩衝就不會截斷。 */
int32_t wkws_frames_for(const WKws* k, int32_t samples);

/* 餵一塊 16-bit PCM，把這塊完成的分數寫進 out（最舊的在前；NaN 表示那一幀還不能判）。
   回寫入的個數（不會超過 cap）。 */
int32_t wkws_push(WKws* k, const int16_t* pcm, int32_t n, float* out, int32_t cap);

/* 抄最近 `samples` 個樣本（最舊的在前；不夠就在前面補零）。回抄到的個數。 */
int32_t wkws_recent(const WKws* k, float* out, int32_t samples);

/* 麥克風重開：忘掉舊音訊。 */
void wkws_reset(WKws* k);

void wkws_destroy(WKws* k);

/* 金標自檢：把人類可讀的報告寫進 out，回 1＝過、0＝不過。 */
int32_t wkws_selftest(const uint8_t* blob, int32_t blen, const uint8_t* golden, int32_t glen,
                      char* out, int32_t out_cap);

/* ── 內建學生聲紋（wake::Spk）──────────────────────────────────────────────────────────────── */

WSpk* wspk_create(const uint8_t* blob, int32_t len, char* err, int32_t err_cap);

/* blob 自帶的操作點。 */
float wspk_threshold(const WSpk* s);

/* 一扇窗對質心的餘弦，[-1, 1]；參數壞掉回 NaN（NaN 一律當「跳過檢查」，不是「拒絕」）。 */
float wspk_cosine(const WSpk* s, const float* window, int32_t n);

void wspk_destroy(WSpk* s);

int32_t wspk_selftest(const uint8_t* blob, int32_t blen, const uint8_t* golden, int32_t glen,
                      char* out, int32_t out_cap);

#ifdef __cplusplus
}   // extern "C"
#endif

#endif   // WAKE_C_API_H
