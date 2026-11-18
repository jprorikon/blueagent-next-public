// iOS 的**螢幕實際大小**（iPhone Duo 展開屏適配，owner 2026-10-30）。
//
// 為什麼要問原生：Unity 的 `Screen` 只有像素，沒有 pt；判斷「大螢幕」要的是 pt。
//
// ⚠️ **不能**用 `[UIScreen mainScreen]`：Duo 上它一直是**外屏**（模擬器實測展開時還回 466 pt）。
// 要問 app 視窗**實際所在**的那塊螢幕（`window.windowScene.screen`）；還沒有視窗才退回 mainScreen。
//
// ⚠️ app 被系統縮成直的一塊時（摺著開、只准直向 ⇒ 展開後縮成中間一條），這裡和 `nativeBounds`
// 都只回縮過的大小（實測 469 pt）——**從 app 裡面量不出真正的內屏**。那一段由 C# 先放開橫向試探
// 解決（`NextLayout.AllowLandscape`），這裡不處理。
//
// ⚠️ 只用 `extern "C"`，C# 側走 `DllImport("__Internal")`（`Next/NextLayout.cs`）。

#import <UIKit/UIKit.h>

extern UIViewController *UnityGetGLViewController(void);

static UIScreen *ba_current_screen(void)
{
    UIWindow *window = UnityGetGLViewController().view.window;
    UIScreen *screen = window.windowScene.screen;
    return screen != nil ? screen : [UIScreen mainScreen];
}

/// 每 pt 幾個像素（Duo 是 3）。
extern "C" float ba_screen_scale(void)
{
    return (float)ba_current_screen().scale;
}

/// app 所在那塊螢幕的短邊（pt）。
extern "C" float ba_screen_short_side(void)
{
    CGSize size = ba_current_screen().bounds.size;
    return (float)MIN(size.width, size.height);
}
