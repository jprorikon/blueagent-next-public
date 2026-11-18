using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>「按住說話」錄到的樣本數（VoiceFrames）：畫面秒數與送出的音檔都靠這一個數。</summary>
    public class VoiceFramesTest
    {
        private const int Rate = 16000;
        private const int Max = 60;
        private const int Full = Rate * Max;

        [Test]
        public void 正常讀數照用()
        {
            Assert.That(VoiceFrames.Recorded(3 * Rate, Full, 3.0f, Max, Rate), Is.EqualTo(3 * Rate));
        }

        [Test]
        public void 讀數回整個緩衝_卡在牆鐘加餘量()
        {
            // 2026-10-29 踩過：按住 3 秒，GetPosition 卻回整個 60 秒緩衝 ⇒ 畫面 60.0s、送出 60 秒
            var frames = VoiceFrames.Recorded(Full, Full, 3.0f, Max, Rate);
            Assert.That(frames, Is.EqualTo((int)((3.0f + VoiceFrames.Slack) * Rate)));
        }

        [Test]
        public void 不超過緩衝全長()
        {
            Assert.That(VoiceFrames.Recorded(Full + 999, Full, 120f, Max, Rate), Is.EqualTo(Full));
        }

        [Test]
        public void 讀數為零_未錄滿當沒錄到_錄滿才算整顆()
        {
            Assert.That(VoiceFrames.Recorded(0, Full, 1.2f, Max, Rate), Is.EqualTo(0));
            Assert.That(VoiceFrames.Recorded(0, Full, 60.5f, Max, Rate), Is.EqualTo(Full));
            Assert.That(VoiceFrames.Recorded(-5, Full, 0.1f, Max, Rate), Is.EqualTo(0));
        }

        [Test]
        public void 沒有緩衝或取樣率就是零()
        {
            Assert.That(VoiceFrames.Recorded(100, 0, 1f, Max, Rate), Is.EqualTo(0));
            Assert.That(VoiceFrames.Recorded(100, Full, 1f, Max, 0), Is.EqualTo(0));
        }
    }
}
