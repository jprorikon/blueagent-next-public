using System;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Motion;

// 量：不同 SelfTransitionBias 下，待機的平均停留時間（選一個「很少換姿勢」的值）
var path = args.Length > 0 ? args[0] : "magic-idle.json";
var data = MagicIdleData.Parse(File.ReadAllText(path));
Console.WriteLine($"資料：{data.Sequence.Frames.Count} 幀 @ {data.Sequence.SampleRateHz}Hz，軸 {data.Axes.Length}");

foreach (var bias in new[] { 6.0, 10.0, 20.0, 40.0, 80.0 })
{
    var started = DateTime.UtcNow;
    var model = ArHmmModel.Fit(data.Sequence, new ArHmmOptions
    {
        StateCount = 3, Order = 6, Ridge = 0.001, Iterations = 3, SelfTransitionBias = bias,
    });
    var ms = (DateTime.UtcNow - started).TotalMilliseconds;
    var d = model.Diagnostics;
    Console.WriteLine($"bias={bias,5}  平均停留 {d.MeanDwellSeconds,6:0.00}s  佔用 " +
                      $"[{string.Join(", ", Array.ConvertAll(d.StateOccupancy, v => v.ToString("0.00")))}]  " +
                      $"擬合 {ms:0} ms");
}
