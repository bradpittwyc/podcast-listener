package com.podcastlearner.tablet;

import android.content.Context;
import android.view.ViewGroup;
import android.webkit.WebView;

/** Process-wide player, owned by the foreground service rather than an Activity. */
final class PlaybackSession {
    private static WebView player;
    static WebView obtain(Context context) {
        if (player == null) player = new WebView(context.getApplicationContext());
        detach(player);
        return player;
    }
    static void detach(WebView view) {
        if (view.getParent() instanceof ViewGroup) ((ViewGroup)view.getParent()).removeView(view);
    }
    static void close() {
        if (player != null) { detach(player); player.destroy(); player = null; }
    }
}
