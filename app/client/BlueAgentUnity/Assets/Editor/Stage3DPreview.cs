using System;
using System.IO;
using BlueAgent.Next;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// **3D 立繪的無頭預覽**（owner 2026-10-30）：每個有模型的角色拍一張，走執行期同一支
    /// `Stage3DRig`（載入 → 收道具 → 接材質 → 放下手臂 → 取景），不必進播放模式、不必裝機。
    ///
    /// 用法（不要配 `-quit`，本支自己 `Exit`）：
    ///   Unity -batchmode -projectPath . -executeMethod BlueAgent.EditorTools.Stage3DPreview.Run
    /// 環境變數：`BA_STAGE3D_CHARS`（逗號分隔，預設 mira,aster,nori）、`BA_STAGE3D_OUT`（預設 Builds/stage3d）、
    /// `BA_STAGE3D_YAW`（轉身角度，預設 0）、`BA_STAGE3D_STATE`（normal／thinking／speaking）。
    /// 出圖尺寸＝聊天頁舞台（460×320）的兩倍。
    /// </summary>
    public static class Stage3DPreview
    {
        private const int Width = 920;
        private const int Height = 640;

        public static void Run()
        {
            var ok = true;
            try
            {
                HeadlessBuild.EnsureRenderPipeline();
                EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

                var outDir = Path.GetFullPath(Environment.GetEnvironmentVariable("BA_STAGE3D_OUT") ?? "Builds/stage3d");
                Directory.CreateDirectory(outDir);
                var characters = (Environment.GetEnvironmentVariable("BA_STAGE3D_CHARS") ?? "mira,aster,nori")
                    .Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries);
                float.TryParse(Environment.GetEnvironmentVariable("BA_STAGE3D_YAW") ?? "0",
                    System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var yaw);
                var state = Environment.GetEnvironmentVariable("BA_STAGE3D_STATE") ?? "normal";
                var face = Environment.GetEnvironmentVariable("BA_STAGE3D_FACE") == "1";
                // 取景：拍到身體的幾成（1＝整個人；見 `Stage3DRig.DefaultShow`）
                if (!float.TryParse(Environment.GetEnvironmentVariable("BA_STAGE3D_SHOW") ?? "",
                        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var show))
                    show = Stage3DRig.DefaultShow;
                // 動作片段（`bamotion/1` JSON）＋拍在第幾秒：驗後端 Kimodo 動作的重定向（`tools/motion_clip.py demo` 產合成片段）
                var motionPath = Environment.GetEnvironmentVariable("BA_STAGE3D_MOTION") ?? "";
                float.TryParse(Environment.GetEnvironmentVariable("BA_STAGE3D_MOTION_T") ?? "1",
                    System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var motionAt);
                var clip = string.IsNullOrEmpty(motionPath) ? null : MotionClip.Parse(File.ReadAllText(motionPath));

                foreach (var raw in characters)
                {
                    var character = raw.Trim();
                    var rig = Stage3DRig.Spawn(character, Vector3.zero, NextStage3D.Layer);
                    var camera = Stage3DRig.NewCamera("Stage3DPreviewCamera", NextStage3D.Layer);
                    camera.backgroundColor = new Color(0.933f, 0.965f, 0.992f, 1f);   // 舞台沒背景圖時的底色
                    try
                    {
                        // 先讓頭髮／袖子在重力下落定（90 步 × 1/60 秒）；BA_STAGE3D_SWING=1 則最後 12 步把人從 0° 轉到 yaw，
                        // 拍的是「甩到一半」那一刻（看慣性有沒有、有沒有穿模）。
                        var swing = Environment.GetEnvironmentVariable("BA_STAGE3D_SWING") == "1";
                        var steps = 90;
                        if (clip != null) steps = 60 + Mathf.RoundToInt(motionAt * 60f);
                        for (var step = 0; step < steps; step++)
                        {
                            if (clip != null && step == 60) rig.PlayMotion(clip);
                            var turned = swing ? yaw * Mathf.Clamp01((step - 77) / 12f) : yaw;
                            rig.Animate(step / 60f, 1f / 60f, turned, state == "thinking" ? 1f : 0f, state == "speaking" ? 1f : 0f);
                        }
                        rig.Aim(camera, (float)Width / Height, show);
                        if (face)
                        {
                            // 臉部特寫：鏡頭抬到頭的高度、視角收窄（看嘴型與臉上的幾層）
                            camera.transform.position += Vector3.up * rig.Bounds.extents.y * 0.33f;
                            camera.fieldOfView = 4.5f;
                        }
                        Stage3DRig.Light(camera);
                        Debug.Log($"[stage3d] {character}：bounds={rig.Bounds.size}／center={rig.Bounds.center}" +
                                  $"／forward={rig.Forward}／camera={camera.transform.position}");
                        var path = Path.Combine(outDir, $"{character}-{(clip != null ? clip.Tag + "@" + motionAt.ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) : state)}-yaw{Mathf.RoundToInt(yaw)}{(face ? "-face" : "")}-show{Mathf.RoundToInt(show * 100)}.png");
                        ok &= HeadlessBuild.CaptureTo(camera, path, Width, Height);
                    }
                    finally
                    {
                        UnityEngine.Object.DestroyImmediate(camera.gameObject);
                        rig.Dispose();
                    }
                }
            }
            catch (Exception error)
            {
                Debug.LogError($"[stage3d] 預覽失敗：{error}");
                ok = false;
            }
            EditorApplication.Exit(ok ? 0 : 1);
        }
    }
}
