using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>審核 OC-1：接聽／拒接的回應只有成功或 404／410 才算處理完；傳輸失敗與 5xx 可以重試。</summary>
    public class IncomingCallReplyTest
    {
        [Test]
        public void SuccessIsDone()
        {
            Assert.That(IncomingCallReply.Classify(true, 200), Is.EqualTo(IncomingCallReply.Kind.Done));
        }

        [TestCase(404)]
        [TestCase(410)]
        public void ServerSaysTheCallIsGone(int status)
        {
            Assert.That(IncomingCallReply.Classify(false, status), Is.EqualTo(IncomingCallReply.Kind.Gone));
        }

        [TestCase(0)]      // 斷網、逾時
        [TestCase(500)]
        [TestCase(502)]
        [TestCase(503)]
        [TestCase(409)]    // 例如她那頭掛不上（busy）：伺服器已轉 failed，輪詢會收掉；這裡不先當成處理完
        public void TransportAndServerErrorsCanBeRetried(int status)
        {
            Assert.That(IncomingCallReply.Classify(false, status), Is.EqualTo(IncomingCallReply.Kind.Retry));
        }
    }
}
