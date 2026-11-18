// 熄屏耳朵的喚醒網／聲紋閘：給 `dev.deepsea.blueagent.shell.BgWake`（Java）的 JNI 薄殼。
//
// 為什麼要有這一支（2026-10-18）：前景那條（C# `WakeNative.cs`）用 P/Invoke 叫
// `libblueagent_wake.so` 的 C 介面（`wake_c_api.h`）與 `libsherpa-onnx-c-api.so`；Unity 一 onStop，
// 主迴圈停 ⇒ C# 的喚醒網就停了，熄屏那段（`BgEars.java`）要自己的一條。Java 不能直接叫 C ⇒ 中間要
// 一層 JNI。**這裡不放任何算術**：每個函式就是 C 介面的一個動作（同 `wake_c_api.h` 的紀律），
// 所以前景與熄屏的分數是同一顆 .so 算出來的。
//
// 連結：`libblueagent_wake.so`、`libsherpa-onnx-c-api.so` 是 NEEDED（同一個 jniLibs 目錄，系統的
// linker 照 app 的 native lib 路徑找）。建置：`unity/tools/build_bgwake_jni.sh`。

#include <jni.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "wake_c_api.h"

// ── sherpa-onnx 的 C API（只宣告用到的那幾支；結構與 `WakeNative.cs` 的 ExtractorConfig 同形）──
typedef struct SherpaOnnxSpeakerEmbeddingExtractorConfig {
    const char* model;
    int32_t num_threads;
    int32_t debug;
    const char* provider;
} SherpaOnnxSpeakerEmbeddingExtractorConfig;

typedef struct SherpaOnnxSpeakerEmbeddingExtractor SherpaOnnxSpeakerEmbeddingExtractor;
typedef struct SherpaOnnxOnlineStream SherpaOnnxOnlineStream;

const SherpaOnnxSpeakerEmbeddingExtractor* SherpaOnnxCreateSpeakerEmbeddingExtractor(
    const SherpaOnnxSpeakerEmbeddingExtractorConfig* config);
void SherpaOnnxDestroySpeakerEmbeddingExtractor(const SherpaOnnxSpeakerEmbeddingExtractor* p);
int32_t SherpaOnnxSpeakerEmbeddingExtractorDim(const SherpaOnnxSpeakerEmbeddingExtractor* p);
const SherpaOnnxOnlineStream* SherpaOnnxSpeakerEmbeddingExtractorCreateStream(
    const SherpaOnnxSpeakerEmbeddingExtractor* p);
const float* SherpaOnnxSpeakerEmbeddingExtractorComputeEmbedding(
    const SherpaOnnxSpeakerEmbeddingExtractor* p, const SherpaOnnxOnlineStream* s);
void SherpaOnnxSpeakerEmbeddingExtractorDestroyEmbedding(const float* v);
void SherpaOnnxOnlineStreamAcceptWaveform(const SherpaOnnxOnlineStream* stream, int32_t sample_rate,
                                          const float* samples, int32_t n);
void SherpaOnnxOnlineStreamInputFinished(const SherpaOnnxOnlineStream* stream);
void SherpaOnnxDestroyOnlineStream(const SherpaOnnxOnlineStream* stream);

#define JNI_FN(name) Java_dev_deepsea_blueagent_shell_BgWake_##name

static void set_error(JNIEnv* env, jobjectArray error, const char* message) {
    if (error == NULL || (*env)->GetArrayLength(env, error) < 1) return;
    jstring s = (*env)->NewStringUTF(env, message);
    (*env)->SetObjectArrayElement(env, error, 0, s);
    (*env)->DeleteLocalRef(env, s);
}

static jfloatArray floats(JNIEnv* env, const float* v, int32_t n) {
    jfloatArray out = (*env)->NewFloatArray(env, n > 0 ? n : 0);
    if (out != NULL && n > 0) (*env)->SetFloatArrayRegion(env, out, 0, n, v);
    return out;
}

// ── 喚醒網（wkws_*）──────────────────────────────────────────────────────────────────────────

JNIEXPORT jlong JNICALL JNI_FN(kwsCreate)(JNIEnv* env, jclass cls, jbyteArray blob, jobjectArray error) {
    (void) cls;
    if (blob == NULL) { set_error(env, error, "blob 是 null"); return 0; }
    jsize n = (*env)->GetArrayLength(env, blob);
    jbyte* bytes = (*env)->GetByteArrayElements(env, blob, NULL);
    char err[512] = {0};
    WKws* k = wkws_create((const uint8_t*) bytes, (int32_t) n, err, (int32_t) sizeof(err));
    (*env)->ReleaseByteArrayElements(env, blob, bytes, JNI_ABORT);
    if (k == NULL) set_error(env, error, err);
    return (jlong) (intptr_t) k;
}

JNIEXPORT jfloat JNICALL JNI_FN(kwsThreshold)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    return h ? wkws_threshold((const WKws*) (intptr_t) h) : 0.0f;
}

JNIEXPORT void JNICALL JNI_FN(kwsDecideEvery)(JNIEnv* env, jclass cls, jlong h, jint every) {
    (void) env; (void) cls;
    if (h) wkws_set_decide_every((WKws*) (intptr_t) h, (int32_t) every);
}

JNIEXPORT void JNICALL JNI_FN(kwsPcmCap)(JNIEnv* env, jclass cls, jlong h, jint samples) {
    (void) env; (void) cls;
    if (h && samples > 0) wkws_set_pcm_cap((WKws*) (intptr_t) h, (int32_t) samples);
}

JNIEXPORT jfloatArray JNICALL JNI_FN(kwsPush)(JNIEnv* env, jclass cls, jlong h, jshortArray pcm, jint n) {
    (void) cls;
    WKws* k = (WKws*) (intptr_t) h;
    if (k == NULL || pcm == NULL || n <= 0) return floats(env, NULL, 0);
    int32_t cap = wkws_frames_for(k, (int32_t) n);
    if (cap < 1) cap = 1;
    float* scores = (float*) malloc(sizeof(float) * (size_t) cap);
    if (scores == NULL) return floats(env, NULL, 0);
    jshort* samples = (*env)->GetShortArrayElements(env, pcm, NULL);
    int32_t got = wkws_push(k, (const int16_t*) samples, (int32_t) n, scores, cap);
    (*env)->ReleaseShortArrayElements(env, pcm, samples, JNI_ABORT);
    jfloatArray out = floats(env, scores, got);
    free(scores);
    return out;
}

JNIEXPORT jfloatArray JNICALL JNI_FN(kwsRecent)(JNIEnv* env, jclass cls, jlong h, jint samples) {
    (void) cls;
    const WKws* k = (const WKws*) (intptr_t) h;
    if (k == NULL || samples <= 0) return floats(env, NULL, 0);
    float* buf = (float*) malloc(sizeof(float) * (size_t) samples);
    if (buf == NULL) return floats(env, NULL, 0);
    int32_t got = wkws_recent(k, buf, (int32_t) samples);
    jfloatArray out = floats(env, buf, got);
    free(buf);
    return out;
}

JNIEXPORT void JNICALL JNI_FN(kwsReset)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    if (h) wkws_reset((WKws*) (intptr_t) h);
}

JNIEXPORT void JNICALL JNI_FN(kwsDestroy)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    if (h) wkws_destroy((WKws*) (intptr_t) h);
}

// ── 內建學生聲紋（wspk_*）────────────────────────────────────────────────────────────────────

JNIEXPORT jlong JNICALL JNI_FN(spkCreate)(JNIEnv* env, jclass cls, jbyteArray blob, jobjectArray error) {
    (void) cls;
    if (blob == NULL) { set_error(env, error, "blob 是 null"); return 0; }
    jsize n = (*env)->GetArrayLength(env, blob);
    jbyte* bytes = (*env)->GetByteArrayElements(env, blob, NULL);
    char err[512] = {0};
    WSpk* s = wspk_create((const uint8_t*) bytes, (int32_t) n, err, (int32_t) sizeof(err));
    (*env)->ReleaseByteArrayElements(env, blob, bytes, JNI_ABORT);
    if (s == NULL) set_error(env, error, err);
    return (jlong) (intptr_t) s;
}

JNIEXPORT jfloat JNICALL JNI_FN(spkThreshold)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    return h ? wspk_threshold((const WSpk*) (intptr_t) h) : 0.0f;
}

JNIEXPORT jfloat JNICALL JNI_FN(spkCosine)(JNIEnv* env, jclass cls, jlong h, jfloatArray window) {
    (void) cls;
    if (h == 0 || window == NULL) return NAN;
    jsize n = (*env)->GetArrayLength(env, window);
    jfloat* w = (*env)->GetFloatArrayElements(env, window, NULL);
    float c = wspk_cosine((const WSpk*) (intptr_t) h, w, (int32_t) n);
    (*env)->ReleaseFloatArrayElements(env, window, w, JNI_ABORT);
    return c;
}

JNIEXPORT void JNICALL JNI_FN(spkDestroy)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    if (h) wspk_destroy((WSpk*) (intptr_t) h);
}

// ── sherpa-onnx 的聲紋 embedding ─────────────────────────────────────────────────────────────

JNIEXPORT jlong JNICALL JNI_FN(sherpaCreate)(JNIEnv* env, jclass cls, jstring model, jint threads, jstring provider) {
    (void) cls;
    if (model == NULL) return 0;
    const char* m = (*env)->GetStringUTFChars(env, model, NULL);
    const char* p = provider != NULL ? (*env)->GetStringUTFChars(env, provider, NULL) : NULL;
    SherpaOnnxSpeakerEmbeddingExtractorConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.model = m;
    cfg.num_threads = threads > 0 ? threads : 1;
    cfg.debug = 0;
    cfg.provider = (p != NULL && p[0] != 0) ? p : "cpu";
    const SherpaOnnxSpeakerEmbeddingExtractor* ex = SherpaOnnxCreateSpeakerEmbeddingExtractor(&cfg);
    (*env)->ReleaseStringUTFChars(env, model, m);
    if (p != NULL) (*env)->ReleaseStringUTFChars(env, provider, p);
    return (jlong) (intptr_t) ex;
}

JNIEXPORT jint JNICALL JNI_FN(sherpaDim)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    return h ? SherpaOnnxSpeakerEmbeddingExtractorDim((const SherpaOnnxSpeakerEmbeddingExtractor*) (intptr_t) h) : 0;
}

/** 一扇窗 ⇒ 原始 embedding（**還沒正規化**；正規化在 Java，與 `SherpaSpkEngine.Embed` 同一步）。失敗回 null。 */
JNIEXPORT jfloatArray JNICALL JNI_FN(sherpaEmbed)(JNIEnv* env, jclass cls, jlong h, jfloatArray window, jint rate) {
    (void) cls;
    const SherpaOnnxSpeakerEmbeddingExtractor* ex = (const SherpaOnnxSpeakerEmbeddingExtractor*) (intptr_t) h;
    if (ex == NULL || window == NULL) return NULL;
    int32_t dim = SherpaOnnxSpeakerEmbeddingExtractorDim(ex);
    const SherpaOnnxOnlineStream* stream = SherpaOnnxSpeakerEmbeddingExtractorCreateStream(ex);
    if (stream == NULL || dim <= 0) return NULL;
    jsize n = (*env)->GetArrayLength(env, window);
    jfloat* w = (*env)->GetFloatArrayElements(env, window, NULL);
    SherpaOnnxOnlineStreamAcceptWaveform(stream, (int32_t) rate, w, (int32_t) n);
    (*env)->ReleaseFloatArrayElements(env, window, w, JNI_ABORT);
    SherpaOnnxOnlineStreamInputFinished(stream);
    const float* e = SherpaOnnxSpeakerEmbeddingExtractorComputeEmbedding(ex, stream);
    jfloatArray out = NULL;
    if (e != NULL) {
        out = floats(env, e, dim);
        SherpaOnnxSpeakerEmbeddingExtractorDestroyEmbedding(e);
    }
    SherpaOnnxDestroyOnlineStream(stream);
    return out;
}

JNIEXPORT void JNICALL JNI_FN(sherpaDestroy)(JNIEnv* env, jclass cls, jlong h) {
    (void) env; (void) cls;
    if (h) SherpaOnnxDestroySpeakerEmbeddingExtractor((const SherpaOnnxSpeakerEmbeddingExtractor*) (intptr_t) h);
}
