package dev.deepsea.blueagent.shell;

import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.LayerDrawable;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;

/**
 * 「看截图」の浮き球と圈選エディタ（原 app `SeeOverlay.kt` の逐行移植）。
 *
 * 流れ：**球を押す → いまの画面を撮る → 圈選 → 一言そえて送る**。
 * 球も圈選画面も `TYPE_ACCESSIBILITY_OVERLAY`（SYSTEM_ALERT_WINDOW 不要）；押したら球を先に隠してから撮る；
 * 圈選は View 一枚（Activity を起こさない）。
 *
 * ⚠️ 原 app との差（ここだけ）：送る先。原 app は同じプロセスの外殼（`ShellController.sendSee`）を
 * 直接叫んで**背景のまま**送れた。Unity は別アプリが前にある間**主ループが止まっている**ので、
 * 切り抜きを {@link ImageStore} に落として `imageSaved`（source=see）を事件佇列に積み、
 * BlueAgent を前に出す ⇒ C# が拉って既存の傳圖路（`/chat` の `image`）で送る。
 */
final class SeeOverlay {
    private static final int BALL_DP = 48;
    private static final long BALL_HIDE_MS = 140L;
    static final int MAX_EDGE = 1600;

    /**
     * 誰が球を出しているか（2026-10-19 路線 A）：無障礙（`TYPE_ACCESSIBILITY_OVERLAY`＋`takeScreenshot`）と
     * 「他のアプリの上に表示」＋`MediaProjection`（`TYPE_APPLICATION_OVERLAY`＋{@link SeeProjection}）の二通り。
     * 球・圈選・送る路は同じ；違うのは窓の種類と「撮る」だけ。
     */
    interface Shooter { void shot(SeeAccessibilityService.ShotCallback cb); }

    private static Context service;
    private static int windowType = WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY;
    private static Shooter shooter;
    private static WindowManager wm;
    private static View ball;
    private static WindowManager.LayoutParams ballParams;
    private static View roi;
    private static Bitmap shot;
    private static boolean appVisible;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private SeeOverlay() {}

    static void attach(SeeAccessibilityService svc) {
        attach(svc, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY, svc::shot);
    }

    /** 球の持ち主を差し替える（後から来た方が勝つ；前の持ち主の球と圈選は片付ける）。 */
    static void attach(Context host, int type, Shooter shot) {
        MAIN.post(() -> {
            hideBall();
            closeRoi();
            service = host;
            windowType = type;
            shooter = shot;
            if (!appVisible) showBall();
        });
    }

    /** `who` が今の持ち主なら外す；無障礙がまだ生きていれば球はそっちに戻す。 */
    static void detach(Context who) {
        MAIN.post(() -> {
            if (service != who) return;
            hideBall();
            closeRoi();
            service = null;
            shooter = null;
            SeeAccessibilityService a11y = SeeAccessibilityService.instance();
            if (a11y != null && a11y != who) attach(a11y);
        });
    }

    /** 我們自己的畫面是不是在前面（activity 生命週期；比無障礙事件可靠）。 */
    static void appVisible(boolean visible) {
        appVisible = visible;
        MAIN.post(() -> { if (visible) hideBall(); else showBall(); });
    }

    static int dp(int v) {
        float d = service != null ? service.getResources().getDisplayMetrics().density : 1f;
        return (int) (v * d);
    }

    // ───────────────────────────────────────────── 浮き球

    private static void showBall() {
        Context svc = service;
        if (svc == null || ball != null || roi != null) return;
        WindowManager mgr = (WindowManager) svc.getSystemService(Context.WINDOW_SERVICE);
        wm = mgr;
        int size = dp(BALL_DP);
        BallView view = new BallView(svc, size);
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
            size, size,
            windowType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.START;
        params.x = 0;
        params.y = dp(220);
        try { mgr.addView(view, params); }
        catch (Exception e) { Log.i(Native.TAG, "看截图：球を出せなかった " + e.getMessage()); }
        ball = view;
        ballParams = params;
    }

    private static void hideBall() {
        View v = ball;
        if (v == null) return;
        try { if (wm != null) wm.removeView(v); } catch (Exception ignored) {}
        ball = null;
        ballParams = null;
    }

    /**
     * 前景換了誰：別的 app ⇒ 放球（圈選中は放さない）。
     *
     * ⚠️ **自分のパッケージのイベントでは收めない**（2026-10-19 真機：圈選で「やめる」⇒ 球が戻らない）。
     * 圈選エディタ（`TYPE_ACCESSIBILITY_OVERLAY`）も自分のパッケージの窓 ⇒ 閉じた瞬間のイベントが
     * 「自分が前に出た」と讀まれて、`showBall()` した直後の球を收めていた。自分の activity が前かどうかは
     * {@link #appVisible}（activity 生命週期）が確實に知っている ⇒ 收球はそっちだけに任せる。
     */
    static void onForegroundPackage(String pkg) {
        Context svc = service;
        if (svc == null) return;
        if (pkg.equals(svc.getPackageName())) {
            if (ball == null && roi == null && !appVisible) Log.i(Native.TAG, "看截图：自分の窓のイベント（球は收めない）");
            return;
        }
        if (!appVisible) showBall();
    }

    private static void snap(int x, int y) {
        WindowManager.LayoutParams params = ballParams;
        if (params == null || service == null) return;
        int size = dp(BALL_DP);
        int w = service.getResources().getDisplayMetrics().widthPixels;
        int h = service.getResources().getDisplayMetrics().heightPixels;
        params.x = (x + size / 2 < w / 2) ? 0 : Math.max(0, w - size);
        params.y = Math.max(0, Math.min(y, Math.max(0, h - size)));
        try { if (wm != null) wm.updateViewLayout(ball, params); } catch (Exception ignored) {}
    }

    /** 浮き球：アプリのアイコンと同じ「輪」。押す＝撮る、押しっぱなしで動かす＝移動。 */
    private static final class BallView extends View {
        private final int size;
        private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
        private float downX, downY;
        private int startX, startY;
        private boolean moved;

        BallView(Context ctx, int size) {
            super(ctx);
            this.size = size;
            fill.setColor(0xFFF88FA8);
            ring.setColor(Color.WHITE);
            ring.setStyle(Paint.Style.STROKE);
            ring.setStrokeWidth(size * 0.075f);
            setAlpha(0.88f);
            setOnTouchListener((v, ev) -> touch(ev));
        }

        private boolean touch(MotionEvent ev) {
            WindowManager.LayoutParams params = ballParams;
            if (params == null) return false;
            switch (ev.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    downX = ev.getRawX(); downY = ev.getRawY();
                    startX = params.x; startY = params.y;
                    moved = false;
                    setAlpha(1f);
                    return true;
                case MotionEvent.ACTION_MOVE: {
                    float dx = ev.getRawX() - downX, dy = ev.getRawY() - downY;
                    if (!moved && (Math.abs(dx) > dp(6) || Math.abs(dy) > dp(6))) moved = true;
                    if (moved) {
                        params.x = startX + (int) dx;
                        params.y = startY + (int) dy;
                        try { if (wm != null) wm.updateViewLayout(this, params); } catch (Exception ignored) {}
                    }
                    return true;
                }
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    setAlpha(0.88f);
                    if (moved) snap(params.x, params.y); else capture();
                    return true;
                default:
                    return false;
            }
        }

        @Override protected void onDraw(Canvas canvas) {
            float r = size / 2f;
            canvas.drawCircle(r, r, r - size * 0.06f, fill);
            canvas.drawCircle(r, r, r - size * 0.20f, ring);
        }
    }

    // ───────────────────────────────────────────── 撮る → 圈選

    private static void capture() {
        final Shooter eye = shooter;
        if (service == null || eye == null) return;
        hideBall();
        MAIN.postDelayed(() -> eye.shot(bmp -> MAIN.post(() -> {
            if (bmp == null) {
                Log.i(Native.TAG, "看截图：撮れなかった（FLAG_SECURE の画面かも）");
                showBall();
                return;
            }
            shot = bmp;
            openRoi();
        })), BALL_HIDE_MS);
    }

    private static void closeRoi() {
        View r = roi;
        if (r != null) { try { if (wm != null) wm.removeView(r); } catch (Exception ignored) {} }
        roi = null;
        shot = null;          // recycle しない（HW レンダラが次のフレームまで握っていることがある）
    }

    /** 自家風格のボタン（主＝天藍亮面＋深藍字、次＝白底＋細框；下に 2dp の「唇」）。 */
    private static Button appButton(Context ctx, String label, boolean primary, View.OnClickListener onClick) {
        int strong = primary ? 0xFF55B8DF : 0xFFCBD9DF;
        GradientDrawable face = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
            primary ? new int[]{0xFFA5E9FF, 0xFF63CBEE} : new int[]{0xFFFFFFFF, 0xFFF1F7FA});
        face.setCornerRadius(dp(4));
        face.setStroke(dp(1), strong);
        GradientDrawable lip = new GradientDrawable();
        lip.setCornerRadius(dp(4));
        lip.setColor(strong);
        LayerDrawable stacked = new LayerDrawable(new Drawable[]{lip, face});
        stacked.setLayerInset(1, 0, 0, 0, dp(2));
        Button b = new Button(ctx);
        b.setText(label);
        b.setTextSize(13f);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setAllCaps(false);
        b.setStateListAnimator(null);
        b.setBackgroundTintList(null);
        b.setMinimumWidth(0);
        b.setMinimumHeight(0);
        b.setMinWidth(0);
        b.setMinHeight(0);
        b.setTextColor(primary ? 0xFF174C74 : 0xFF47617D);
        b.setPadding(dp(10), 0, dp(10), dp(2));
        b.setBackground(stacked);
        b.setOnClickListener(onClick);
        return b;
    }

    private static void openRoi() {
        final Context svc = service;
        final Bitmap bmp = shot;
        WindowManager mgr = wm;
        if (svc == null || bmp == null || mgr == null) return;

        LinearLayout root = new LinearLayout(svc);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xCC000000);
        root.setPadding(0, dp(46), 0, 0);
        final RoiView editor = new RoiView(svc, bmp);

        LinearLayout bar = new LinearLayout(svc);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setPadding(dp(10), dp(8), dp(10), dp(8));
        bar.setBackgroundColor(0xFF101418);
        final EditText input = new EditText(svc);
        input.setHint("この画面について一言（空でもOK）");
        input.setTextColor(Color.WHITE);
        input.setHintTextColor(0xFF6B7A88);
        input.setTextSize(15f);
        input.setSingleLine(true);
        Button send = appButton(svc, "送る", true, v -> {
            hideKeyboard(input);
            String text = input.getText().toString().trim();
            Bitmap cropped = editor.crop();
            closeRoi();
            if (cropped != null) handOff(svc, cropped, text);
            showBall();
        });
        Button cancel = appButton(svc, "やめる", false, v -> {
            hideKeyboard(input);
            closeRoi();
            showBall();
        });
        bar.addView(input, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        LinearLayout.LayoutParams lp1 = new LinearLayout.LayoutParams(dp(74), dp(40));
        lp1.setMarginStart(dp(6));
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(dp(74), dp(40));
        lp2.setMarginStart(dp(6));
        bar.addView(cancel, lp1);
        bar.addView(send, lp2);
        // バーは**上**（下に置くと IME が被さる——原 app 2026-10-03 実測）。
        root.addView(bar);
        root.addView(editor, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT,
            windowType,
            // FLAG_NOT_FOCUSABLE を付けない（付けると EditText に入力できない）
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.START;
        params.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
            | WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN;
        try { mgr.addView(root, params); }
        catch (Exception e) { Log.i(Native.TAG, "看截图：圈選画面を出せなかった " + e.getMessage()); }
        roi = root;
    }

    private static void hideKeyboard(View v) {
        if (service == null) return;
        InputMethodManager imm = (InputMethodManager) service.getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.hideSoftInputFromWindow(v.getWindowToken(), 0);
    }

    /**
     * 切り抜きを落として C# に渡す（`imageSaved`，source=see，caption＝その一言；空なら
     * 「（この画面を見せた）」——原 app `sendSee` と同じ既定文）、BlueAgent を前に出す。
     */
    private static void handOff(final Context svc, final Bitmap bmp, final String prompt) {
        if (Native.app == null) Native.app = svc.getApplicationContext();
        Native.WORKER.execute(() -> {
            String id = ImageStore.save(svc, bmp);
            Native.seeSaved(svc, id, prompt.isEmpty() ? "（この画面を見せた）" : prompt);
            Log.i(Native.TAG, "看截图：送る（" + (id == null ? "保存失敗" : id) + "）prompt=" + prompt);
        });
        try {
            Intent launch = svc.getPackageManager().getLaunchIntentForPackage(svc.getPackageName());
            if (launch != null) {
                launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                svc.startActivity(launch);
            }
        } catch (Exception e) {
            Log.i(Native.TAG, "看截图：BlueAgent を前に出せなかった " + e.getMessage());
        }
    }

    // ───────────────────────────────────────────── 圈選する View

    /** 画面を敷いて、暗幕＋四隅ドラッグの矩形。初期値は中央 60%、角の当たり判定は 32dp。 */
    private static final class RoiView extends View {
        private final Bitmap bmp;
        private final RectF dst = new RectF();
        private final RectF sel = new RectF();
        private final Paint dim = new Paint();
        private final Paint edge = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint handle = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Matrix matrix = new Matrix();
        private int mode;                      // 0=なし 1=移動 2..5=角
        private float lastX, lastY;

        RoiView(Context ctx, Bitmap bmp) {
            super(ctx);
            this.bmp = bmp;
            dim.setColor(0xAA000000);
            edge.setColor(Color.WHITE);
            edge.setStyle(Paint.Style.STROKE);
            edge.setStrokeWidth(3f);
            handle.setColor(0xFFF88FA8);
        }

        private float slop() { return 32f * getResources().getDisplayMetrics().density; }

        @Override protected void onSizeChanged(int w, int h, int oldw, int oldh) {
            super.onSizeChanged(w, h, oldw, oldh);
            float scale = Math.min(w / (float) bmp.getWidth(), h / (float) bmp.getHeight());
            float dw = bmp.getWidth() * scale, dh = bmp.getHeight() * scale;
            float left = (w - dw) / 2f, top = (h - dh) / 2f;
            dst.set(left, top, left + dw, top + dh);
            matrix.setRectToRect(new RectF(0, 0, bmp.getWidth(), bmp.getHeight()), dst, Matrix.ScaleToFit.CENTER);
            if (sel.isEmpty()) sel.set(dst.left + dw * 0.2f, dst.top + dh * 0.2f, dst.right - dw * 0.2f, dst.bottom - dh * 0.2f);
        }

        @Override protected void onDraw(Canvas canvas) {
            canvas.drawBitmap(bmp, matrix, null);
            canvas.drawRect(dst.left, dst.top, dst.right, sel.top, dim);
            canvas.drawRect(dst.left, sel.bottom, dst.right, dst.bottom, dim);
            canvas.drawRect(dst.left, sel.top, sel.left, sel.bottom, dim);
            canvas.drawRect(sel.right, sel.top, dst.right, sel.bottom, dim);
            canvas.drawRect(sel, edge);
            float s = slop() * 0.42f;
            float[][] c = corners();
            for (float[] p : c) canvas.drawCircle(p[0], p[1], s, handle);
        }

        private float[][] corners() {
            return new float[][]{{sel.left, sel.top}, {sel.right, sel.top}, {sel.left, sel.bottom}, {sel.right, sel.bottom}};
        }

        @Override public boolean onTouchEvent(MotionEvent ev) {
            float x = ev.getX(), y = ev.getY(), s = slop();
            switch (ev.getActionMasked()) {
                case MotionEvent.ACTION_DOWN: {
                    mode = 0;
                    float[][] c = corners();
                    for (int i = 0; i < c.length; i++)
                        if (Math.hypot(x - c[i][0], y - c[i][1]) < s) mode = i + 2;
                    if (mode == 0 && sel.contains(x, y)) mode = 1;
                    lastX = x; lastY = y;
                    return mode != 0;
                }
                case MotionEvent.ACTION_MOVE: {
                    if (mode == 0) return false;
                    float dx = x - lastX, dy = y - lastY;
                    lastX = x; lastY = y;
                    switch (mode) {
                        case 1: {
                            float nx = dx, ny = dy;
                            if (sel.left + nx < dst.left) nx = dst.left - sel.left;
                            if (sel.right + nx > dst.right) nx = dst.right - sel.right;
                            if (sel.top + ny < dst.top) ny = dst.top - sel.top;
                            if (sel.bottom + ny > dst.bottom) ny = dst.bottom - sel.bottom;
                            sel.offset(nx, ny);
                            break;
                        }
                        case 2: grow(x, y, false, false); break;
                        case 3: grow(x, y, true, false); break;
                        case 4: grow(x, y, false, true); break;
                        case 5: grow(x, y, true, true); break;
                    }
                    invalidate();
                    return true;
                }
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    mode = 0;
                    return true;
            }
            return false;
        }

        private void grow(float x, float y, boolean right, boolean bottom) {
            float min = slop() * 1.5f;
            if (right) sel.right = clamp(x, sel.left + min, dst.right); else sel.left = clamp(x, dst.left, sel.right - min);
            if (bottom) sel.bottom = clamp(y, sel.top + min, dst.bottom); else sel.top = clamp(y, dst.top, sel.bottom - min);
        }

        private static float clamp(float v, float lo, float hi) { return Math.max(lo, Math.min(hi, v)); }

        /** 圈選部分を切り出して、長辺 MAX_EDGE まで縮めて返す。 */
        Bitmap crop() {
            if (sel.isEmpty() || dst.isEmpty()) return null;
            float sx = bmp.getWidth() / dst.width(), sy = bmp.getHeight() / dst.height();
            int x = (int) Math.max(0, Math.min(bmp.getWidth() - 1, (sel.left - dst.left) * sx));
            int y = (int) Math.max(0, Math.min(bmp.getHeight() - 1, (sel.top - dst.top) * sy));
            int w = (int) Math.max(1, Math.min(bmp.getWidth() - x, sel.width() * sx));
            int h = (int) Math.max(1, Math.min(bmp.getHeight() - y, sel.height() * sy));
            Bitmap cut;
            try { cut = Bitmap.createBitmap(bmp, x, y, w, h); } catch (Exception e) { return null; }
            int longEdge = Math.max(cut.getWidth(), cut.getHeight());
            if (longEdge <= MAX_EDGE) return cut;
            float k = MAX_EDGE / (float) longEdge;
            return Bitmap.createScaledBitmap(cut, Math.max(1, (int) (cut.getWidth() * k)),
                Math.max(1, (int) (cut.getHeight() * k)), true);
        }
    }
}
