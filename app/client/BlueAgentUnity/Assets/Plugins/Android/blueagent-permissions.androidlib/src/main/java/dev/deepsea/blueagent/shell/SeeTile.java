package dev.deepsea.blueagent.shell;

import android.app.PendingIntent;
import android.content.Intent;
import android.os.Build;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * クイック設定のタイル「看截图」（路線 A の入口）：押す ⇒ {@link SeeProjectionActivity}
 * （未許可なら設定へ／撮影中なら止める／それ以外は系統の共有確認）。點灯＝撮影セッション中。
 */
public class SeeTile extends TileService {
    @Override
    public void onStartListening() {
        super.onStartListening();
        Tile tile = getQsTile();
        if (tile == null) return;
        tile.setState(SeeProjection.running() ? Tile.STATE_ACTIVE : Tile.STATE_INACTIVE);
        tile.updateTile();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onClick() {
        super.onClick();
        Intent open = new Intent(this, SeeProjectionActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE));
        } else {
            startActivityAndCollapse(open);
        }
    }
}
