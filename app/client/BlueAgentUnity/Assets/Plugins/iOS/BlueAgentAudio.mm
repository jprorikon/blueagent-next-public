// iOS 的**音訊會話**（Unity 的 Microphone 不會自己把 session 設成「可錄音」）。
//
// 為什麼一定要這一支（2026-10-19 真機實測）：
//   `Microphone.Start()` 在 iOS 上**不會報錯**，日誌也照樣印「開麥：iPhone audio input」
//   （2026-10-19 的 app.log 就是這樣），但 `Microphone.GetPosition()` 永遠回 0 ⇒ 一塊音訊都拿不到
//   （同一份日誌裡 `[wake] 探針：30 s 內 0 塊（沒在餵）` 就是這個）。
//   根因＝**AVAudioSession 沒有設成 PlayAndRecord 並 setActive**：預設 category 下錄音拿不到資料，
//   而 Unity 的 C# 層完全看不到這件事（沒有例外、沒有回傳值）。
//
// 做法：開錄前 `ba_audio_session_begin()`（PlayAndRecord ＋ DefaultToSpeaker ＋ 允許藍牙 ＋
// MixWithOthers，讓她說話時其他 app 的聲音不被硬切），收工 `ba_audio_session_end()`。
// ⚠️ 只用 `extern "C"`，C# 側走 `DllImport("__Internal")`（靜態連結進 app，不是 dylib）。

#import <AVFoundation/AVFoundation.h>

extern "C" void ba_audio_session_begin(void)
{
    AVAudioSession *session = [AVAudioSession sharedInstance];
    NSError *error = nil;

    // PlayAndRecord：要同時收音與播放（她是「一直听 + 会说话」）。
    // DefaultToSpeaker：不設的話聲音會走聽筒（很小聲）。
    // AllowBluetooth：藍牙耳機。
    // MixWithOthers：不要硬切斷別的 app 的音訊。
    AVAudioSessionCategoryOptions options =
        AVAudioSessionCategoryOptionDefaultToSpeaker |
        AVAudioSessionCategoryOptionAllowBluetooth |
        AVAudioSessionCategoryOptionMixWithOthers;

    if (![session setCategory:AVAudioSessionCategoryPlayAndRecord
                  withOptions:options
                        error:&error]) {
        NSLog(@"[ba-audio] setCategory 失敗：%@", error.localizedDescription);
        error = nil;
    }
    // 44.1k 是硬體常見值；Unity 的 Microphone 會自己重採樣到 16k，這裡不硬碰。
    if (![session setPreferredSampleRate:44100.0 error:&error]) {
        NSLog(@"[ba-audio] setPreferredSampleRate 失敗：%@", error.localizedDescription);
        error = nil;
    }
    if (![session setActive:YES withOptions:0 error:&error]) {
        NSLog(@"[ba-audio] setActive 失敗：%@", error.localizedDescription);
    }
    // ⚠️ **一定要再 override 一次輸出埠到喇叭**（2026-10-19 真機：「能听到但音量很小」）。
    //    只設 DefaultToSpeaker **不夠**：PlayAndRecord 下 iOS 會把輸出留在**聽筒**（音量小、貼耳才聽得清），
    //    尤其在有藍牙/耳機接過、或 category 被改過之後。`PortOverrideSpeaker` 才是硬拉回喇叭。
    if (![session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:&error]) {
        NSLog(@"[ba-audio] overrideOutputAudioPort 失敗：%@", error.localizedDescription);
        error = nil;
    }
    NSMutableString *outs = [NSMutableString string];
    for (AVAudioSessionPortDescription *p in session.currentRoute.outputs)
        [outs appendFormat:@"%@(%@) ", p.portType, p.portName];
    NSLog(@"[ba-audio] session 已開：category=%@ options=%lu sampleRate=%.0f 輸出=%@ 音量=%.2f",
          session.category, (unsigned long)session.categoryOptions, session.sampleRate,
          outs, session.outputVolume);
}


extern "C" void ba_audio_session_end(void)
{
    NSError *error = nil;
    [[AVAudioSession sharedInstance] setActive:NO
                                   withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation
                                         error:&error];
    if (error) NSLog(@"[ba-audio] setActive(NO) 失敗：%@", error.localizedDescription);
}

// 每次播放前**再拉一次**喇叭（Unity/FMOD 会在输出开始时重设 session ⇒ 我们开机设的 override 会被冲掉，
// 症状就是「明明 override 过、声音还是从听筒出来」）。
extern "C" void ba_audio_force_speaker(void)
{
    AVAudioSession *session = [AVAudioSession sharedInstance];
    NSError *error = nil;
    if (![session overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker error:&error]) {
        NSLog(@"[ba-audio] force speaker 失敗：%@", error.localizedDescription);
    }
}

// 把「当前输出端口／类别／音量」写进 buf，让 C# 侧能印进 app.log（NSLog 进不了那个文件）。
extern "C" void ba_audio_route_info(char *buf, int cap)
{
    if (buf == NULL || cap <= 0) return;
    AVAudioSession *session = [AVAudioSession sharedInstance];
    NSMutableString *s = [NSMutableString string];
    for (AVAudioSessionPortDescription *p in session.currentRoute.outputs)
        [s appendFormat:@"%@(%@) ", p.portType, p.portName];
    snprintf(buf, (size_t)cap, "cat=%s out=%s vol=%.2f",
             [session.category UTF8String], [s UTF8String], session.outputVolume);
}
