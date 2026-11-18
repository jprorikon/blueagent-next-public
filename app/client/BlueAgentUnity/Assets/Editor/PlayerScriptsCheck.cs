// ⚠️ 不要包 `#if UNITY_IOS`／`UNITY_ANDROID`：理由同 `IosPlistPostProcess.cs`（批處理時 active target 未必是那一端）。
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Player;
using UnityEngine;

namespace BlueAgent.EditorTools
{
    /// <summary>
    /// **玩家端腳本編譯閘門**（2026-11-17）：只編 Android／iOS 玩家那一份 C#，不出包、不跑 IL2CPP。
    ///
    /// 為什麼要有：`-batchmode -quit` 的編譯閘門只編**編輯器**那一份——`#if UNITY_ANDROID &amp;&amp; !UNITY_EDITOR`、
    /// `#if UNITY_IOS &amp;&amp; !UNITY_EDITOR` 裡的原生橋接（`NativeShell`、`NextLayout`…）在編輯器裡被排除，
    /// 寫錯了要到真出包才發現（Android IL2CPP 一次十幾分鐘、iOS 還要再進 Xcode）。
    ///
    /// 用法（exit 0＝兩端都乾淨；有錯時 `error CS` 照常進 log）：
    ///   Unity -batchmode -quit -nographics -projectPath . -executeMethod BlueAgent.EditorTools.PlayerScriptsCheck.Run -logFile …
    /// 只編一端：`-executeMethod BlueAgent.EditorTools.PlayerScriptsCheck.Android`（或 `.IOS`）。
    /// </summary>
    public static class PlayerScriptsCheck
    {
        public static void Run() => Exit(Compile(BuildTarget.Android) & Compile(BuildTarget.iOS));

        public static void Android() => Exit(Compile(BuildTarget.Android));

        public static void IOS() => Exit(Compile(BuildTarget.iOS));

        private static bool Compile(BuildTarget target)
        {
            var group = BuildPipeline.GetBuildTargetGroup(target);
            if (!BuildPipeline.IsBuildTargetSupported(group, target))
            {
                Debug.LogError($"[player-scripts] 這台 Unity 沒裝 {target} 模組");
                return false;
            }
            var output = Path.Combine(Path.GetTempPath(), "ba-player-scripts-" + target);
            if (Directory.Exists(output)) Directory.Delete(output, true);
            var settings = new ScriptCompilationSettings { target = target, group = group };
            var result = PlayerBuildInterface.CompilePlayerScripts(settings, output);
            var names = result.assemblies?.ToArray() ?? Array.Empty<string>();
            var ok = names.Any(name => name.StartsWith("Assembly-CSharp", StringComparison.Ordinal));
            Debug.Log(ok
                ? $"[player-scripts] {target}：✓ {names.Length} 個組件（{string.Join(", ", names.Where(n => n.StartsWith("Assembly-CSharp") || n.StartsWith("BlueAgent")))}）"
                : $"[player-scripts] {target}：✗ 沒編出 Assembly-CSharp（看上面的 error CS）");
            return ok;
        }

        private static void Exit(bool ok)
        {
            if (Application.isBatchMode) EditorApplication.Exit(ok ? 0 : 1);
        }
    }
}
