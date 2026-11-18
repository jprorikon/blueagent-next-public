using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// `AvatarTimeline`（＝真相 `assets/webui/avatar.js`）的逐條對拍。
    ///
    /// 測資用**真的角色卡形狀**（`live2d/assets/characters/aster/character.json` 的縮版）：
    /// `normal` 3100/110/180/90/4200、`thinking` 2700/120/3900、`speaking` `transition_ms: 0` ＋
    /// 五個嘴型框（`a`／`i`／`u-v2`／`e`／`o`）。
    /// </summary>
    public class AvatarTimelineTest
    {
        /// <summary>有 animations 的卡（現役：`live2d/assets/characters/<id>/character.json`）。</summary>
        const string Aster = @"{
          ""id"": ""aster"", ""name"": ""アスター"", ""short_name"": ""ASTER"",
          ""images"": {
            ""normal"": ""animated-normal.png"",
            ""speaking"": ""animated-mouth-o.png"",
            ""thinking"": ""animated-thinking.png""
          },
          ""animations"": {
            ""normal"": { ""frames"": [
              { ""file"": ""animated-normal.png"", ""duration_ms"": 3100 },
              { ""file"": ""animated-blink.png"", ""duration_ms"": 110 },
              { ""file"": ""animated-normal.png"", ""duration_ms"": 180 },
              { ""file"": ""animated-blink.png"", ""duration_ms"": 90 },
              { ""file"": ""animated-normal.png"", ""duration_ms"": 4200 }
            ] },
            ""thinking"": { ""frames"": [
              { ""file"": ""animated-thinking.png"", ""duration_ms"": 2700 },
              { ""file"": ""animated-thinking-blink.png"", ""duration_ms"": 120 },
              { ""file"": ""animated-thinking.png"", ""duration_ms"": 3900 }
            ] },
            ""speaking"": { ""transition_ms"": 0, ""frames"": [
              { ""file"": ""animated-mouth-a.png"", ""duration_ms"": 4000 },
              { ""file"": ""animated-mouth-i.png"", ""duration_ms"": 4000 },
              { ""file"": ""animated-mouth-u-v2.png"", ""duration_ms"": 4000 },
              { ""file"": ""animated-mouth-e.png"", ""duration_ms"": 4000 },
              { ""file"": ""animated-mouth-o.png"", ""duration_ms"": 4000 }
            ] }
          }
        }";

        /// <summary>舊式卡（`blueagent/characters/<id>/character.json`）：只有三張圖、沒有 animations。</summary>
        const string Plain = @"{
          ""id"": ""aster"", ""name"": ""アスター"",
          ""images"": { ""normal"": ""normal.png"", ""speaking"": ""speaking.png"", ""thinking"": ""thinking.png"" }
        }";

        /// <summary>`speaking` 表裡**缺** `e` 那一行（規則 3 的測試用）。</summary>
        const string Sparse = @"{
          ""id"": ""aster"",
          ""images"": { ""normal"": ""animated-normal.png"", ""speaking"": ""animated-mouth-a.png"" },
          ""animations"": { ""speaking"": { ""transition_ms"": 0, ""frames"": [
            { ""file"": ""animated-mouth-a.png"", ""duration_ms"": 4000 },
            { ""file"": ""animated-mouth-u-v2.png"", ""duration_ms"": 4000 }
          ] } }
        }";

        // ── 時間軸輪播（normal／thinking）────────────────────────────────────

        [Test]
        public void Normal_第一格就是_images_normal()
        {
            var a = new AvatarTimeline(Aster, "aster");
            Assert.That(a.State, Is.EqualTo(AvatarTimeline.Normal));
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));
        }

        [Test]
        public void Normal_按_durationMs_輪播_眨眼那兩格會出現又收掉()
        {
            var a = new AvatarTimeline(Aster, "aster");

            a.Advance(3099);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));      // 還沒到 3100

            a.Advance(3100);
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));       // 3100~3210 眨眼

            a.Advance(3209);
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));

            a.Advance(3210);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));      // 180ms 的長開

            a.Advance(3390);
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));       // 第二次眨（90ms）

            a.Advance(3480);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));

            a.Advance(3480 + 4199);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));

            // 3480 + 4200 = 7680＝一輪走完 ⇒ 繞回**第 0 格**（normal），不是 blink
            a.Advance(3480 + 4200);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));

            a.Advance(3480 + 4200 + 3100);                               // 再過 3100 ⇒ 第二輪的眨眼
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));
        }

        [Test]
        public void 一次跳很大一截也會落在正確的格子上()
        {
            var a = new AvatarTimeline(Aster, "aster");
            // 3100 + 110 + 180 + 90 + 4200 = 7680（一輪）；7680+3100 = 10780 ⇒ 第二輪的 blink
            a.Advance(10780);
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));
        }

        [Test]
        public void Thinking_有自己的三格()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Thinking);
            Assert.That(a.Src, Is.EqualTo("animated-thinking.png"));

            a.Advance(2700);
            Assert.That(a.Src, Is.EqualTo("animated-thinking-blink.png"));

            a.Advance(2820);
            Assert.That(a.Src, Is.EqualTo("animated-thinking.png"));

            // 2820 + 3900 = 6720＝一輪走完 ⇒ 繞回第 0 格（thinking）
            a.Advance(2820 + 3900);
            Assert.That(a.Src, Is.EqualTo("animated-thinking.png"));

            a.Advance(2820 + 3900 + 2700);                                     // 第二輪的眨眼
            Assert.That(a.Src, Is.EqualTo("animated-thinking-blink.png"));
        }

        [Test]
        public void 同一個狀態再設一次_不重啟動畫_memo()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.Advance(3100);
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));

            a.SetState(AvatarTimeline.Normal);                    // 同一組 frames ⇒ 直接 return
            Assert.That(a.Src, Is.EqualTo("animated-blink.png"));            // 沒有被打回第一格

            a.Advance(3210);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));          // 節奏照舊往下走
        }

        // ── speaking：查表、不輪播（規則 2＋4）──────────────────────────────

        [Test]
        public void Speaking_進來就停在代表框_等_lip()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-o.png"));          // ＝ images.speaking

            a.Advance(99999);                                     // 沒有 lip 訊息就一直停著
            Assert.That(a.Src, Is.EqualTo("animated-mouth-o.png"));
            Assert.That(a.LipDriven, Is.False);
        }

        [Test]
        public void 嘴型查表_a_i_u_v2_e_o_都找得到()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);

            Assert.That(a.ApplyLip("a"), Is.True);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-a.png"));
            Assert.That(a.LipDriven, Is.True);

            Assert.That(a.ApplyLip("i"), Is.True);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-i.png"));

            Assert.That(a.ApplyLip("u"), Is.True);                          // 表裡只有 -v2 那張
            Assert.That(a.Src, Is.EqualTo("animated-mouth-u-v2.png"));

            Assert.That(a.ApplyLip("e"), Is.True);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-e.png"));

            Assert.That(a.ApplyLip("o"), Is.True);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-o.png"));
        }

        [Test]
        public void 嘴型是瞬間的_transition_永遠是_0()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            a.ApplyLip("a");
            Assert.That(a.TransitionMs, Is.EqualTo(0.0));
        }

        [Test]
        public void 嘴型收到後_推時間不會把它蓋掉()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            a.ApplyLip("i");
            a.Advance(5000);
            a.Advance(60000);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-i.png"));
        }

        [Test]
        public void closed_畫_images_normal()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            Assert.That(a.ApplyLip(AvatarTimeline.Closed), Is.True);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));
        }

        [Test]
        public void 不在形狀表裡的嘴型不收()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            Assert.That(a.ApplyLip("z"), Is.False);
            Assert.That(a.ApplyLip(""), Is.False);
            Assert.That(a.LipDriven, Is.False);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-o.png"));          // 原圖留著
        }

        [Test]
        public void 非_speaking_狀態不吃嘴型()
        {
            var a = new AvatarTimeline(Aster, "aster");
            Assert.That(a.ApplyLip("a"), Is.False);
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));
        }

        [Test]
        public void 表上缺那一行就什麼都不畫_原圖留著()
        {
            var a = new AvatarTimeline(Sparse, "aster");
            a.SetState(AvatarTimeline.Speaking);
            Assert.That(a.Src, Is.EqualTo("animated-mouth-a.png"));           // ＝ images.speaking

            Assert.That(a.ApplyLip("e"), Is.False);                         // 表裡沒有 e
            Assert.That(a.Src, Is.EqualTo("animated-mouth-a.png"));           // 畫面留在原圖
            Assert.That(a.LipDriven, Is.True);                              // 旗標照樣立（avatar.js 的順序）

            Assert.That(a.ApplyLip("u"), Is.True);                          // 有的就找得到
            Assert.That(a.Src, Is.EqualTo("animated-mouth-u-v2.png"));
        }

        // ── 換角色 ───────────────────────────────────────────────────────────

        [Test]
        public void 換角色回到_normal_並重設_memo_與_lip()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.SetState(AvatarTimeline.Speaking);
            a.ApplyLip("a");
            Assert.That(a.LipDriven, Is.True);

            a.SetCharacter("aster");                                 // 同一個 id 也要重設
            Assert.That(a.State, Is.EqualTo(AvatarTimeline.Normal));
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));
            Assert.That(a.LipDriven, Is.False);
        }

        [Test]
        public void 清單裡沒有這個_id_時_退回第一張卡()
        {
            var a = new AvatarTimeline(Aster, "nobody");
            Assert.That(a.HasProfile, Is.True);                             // ＝第一張卡（avatar.js 的 || list()[0]）
            Assert.That(a.Src, Is.EqualTo("animated-normal.png"));
        }

        // ── 沒有 animations 的舊式卡（規則 4 的原形）────────────────────────

        [Test]
        public void 舊式卡_normal_固定一張()
        {
            var a = new AvatarTimeline(Plain, "aster");
            Assert.That(a.Src, Is.EqualTo("normal.png"));
            a.Advance(99999);
            Assert.That(a.Src, Is.EqualTo("normal.png"));                     // 單張、1000ms 繞回自己
        }

        [Test]
        public void 舊式卡_speaking_停在_speaking_那一張_且不吃嘴型()
        {
            var a = new AvatarTimeline(Plain, "aster");
            a.SetState(AvatarTimeline.Speaking);
            Assert.That(a.Src, Is.EqualTo("speaking.png"));
            Assert.That(a.ApplyLip("a"), Is.False);                         // 沒有 speaking 表 ⇒ 找不列
            Assert.That(a.Src, Is.EqualTo("speaking.png"));
        }

        [Test]
        public void 舊式卡_thinking_固定一張()
        {
            var a = new AvatarTimeline(Plain, "aster");
            a.SetState(AvatarTimeline.Thinking);
            Assert.That(a.Src, Is.EqualTo("thinking.png"));
        }

        // ── 邊界 ─────────────────────────────────────────────────────────────

        [Test]
        public void 沒有角色卡_不畫()
        {
            var a = new AvatarTimeline("[]", "aster");
            Assert.That(a.HasProfile, Is.False);
            Assert.That(a.Src, Is.Null);
            a.Advance(100);
            Assert.That(a.Src, Is.Null);
        }

        [Test]
        public void 空的_durationMs_用預設_167()
        {
            const string json = @"{
              ""id"": ""x"", ""images"": { ""normal"": ""n.png"" },
              ""animations"": { ""normal"": { ""frames"": [
                { ""file"": ""n.png"" }, { ""file"": ""b.png"" }
              ] } }
            }";
            var a = new AvatarTimeline(json, "x");
            Assert.That(a.Src, Is.EqualTo("n.png"));
            a.Advance(166);
            Assert.That(a.Src, Is.EqualTo("n.png"));
            a.Advance(167);
            Assert.That(a.Src, Is.EqualTo("b.png"));
        }

        [Test]
        public void durationMs_駝峰寫法也吃()
        {
            const string json = @"{
              ""id"": ""x"", ""images"": { ""normal"": ""n.png"" },
              ""animations"": { ""normal"": { ""frames"": [
                { ""src"": ""n.png"", ""durationMs"": 500 }, { ""src"": ""b.png"", ""durationMs"": 500 }
              ] } }
            }";
            var a = new AvatarTimeline(json, "x");
            Assert.That(a.Src, Is.EqualTo("n.png"));
            a.Advance(500);
            Assert.That(a.Src, Is.EqualTo("b.png"));
        }

        [Test]
        public void 角色清單是陣列時_按_id_挑卡()
        {
            var array = "[" + Aster.Replace(@"""id"": ""aster""", @"""id"": ""aster""") + "," +
                        Plain.Replace(@"""id"": ""aster""", @"""id"": ""mira""") + "]";
            var a = new AvatarTimeline(array, "mira");
            Assert.That(a.Src, Is.EqualTo("normal.png"));                     // 拿到 mira 那張舊卡
        }

        [Test]
        public void Stop_之後不畫()
        {
            var a = new AvatarTimeline(Aster, "aster");
            a.Stop();
            Assert.That(a.Src, Is.Null);
            a.Advance(100);
            Assert.That(a.Src, Is.Null);
        }
    }
}
