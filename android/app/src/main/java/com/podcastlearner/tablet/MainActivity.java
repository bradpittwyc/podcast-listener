package com.podcastlearner.tablet;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.view.Gravity;
import android.graphics.Typeface;
import android.graphics.LinearGradient;
import android.graphics.Shader;
import android.graphics.drawable.GradientDrawable;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    private WebView web;
    private FrameLayout layout;
    private FrameLayout splash;
    private ProgressBar loading;
    private long splashStarted;
    private TextView status;
    private Button retry;
    private final String server = "http://127.0.0.1:8557";
    private boolean pageLoaded, pageFailed;
    private volatile boolean destroyed;
    private final java.util.concurrent.atomic.AtomicBoolean sharing = new java.util.concurrent.atomic.AtomicBoolean();
    private final java.util.concurrent.atomic.AtomicBoolean connecting = new java.util.concurrent.atomic.AtomicBoolean();

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (getResources().getConfiguration().smallestScreenWidthDp < 600) {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        }
        getWindow().setStatusBarColor(0xff0d0d18);
        getWindow().setNavigationBarColor(0xff0d0d18);
        layout = new FrameLayout(this);
        layout.setBackgroundColor(0xff09090f);
        splashStarted=android.os.SystemClock.uptimeMillis();
        splash=new FrameLayout(this);
        splash.setBackground(new GradientDrawable(GradientDrawable.Orientation.TL_BR,new int[]{0xff1b102c,0xff0d0d18,0xff09090f}));
        LinearLayout brand=new LinearLayout(this);
        brand.setOrientation(LinearLayout.VERTICAL);brand.setGravity(Gravity.CENTER);
        ImageView icon=new ImageView(this);icon.setImageResource(R.drawable.ic_podcast);
        GradientDrawable iconShape=new GradientDrawable();iconShape.setColor(0xff15101f);iconShape.setCornerRadius(dp(26));iconShape.setStroke(dp(1),0xff40234f);
        icon.setBackground(iconShape);icon.setClipToOutline(true);icon.setElevation(dp(12));
        brand.addView(icon,new LinearLayout.LayoutParams(dp(96),dp(96)));
        TextView appName=new TextView(this){
            @Override protected void onSizeChanged(int w,int h,int oldw,int oldh){
                super.onSizeChanged(w,h,oldw,oldh);
                getPaint().setShader(new LinearGradient(0,0,w,h,new int[]{0xffffffff,0xffe9d5ff,0xffc084fc},null,Shader.TileMode.CLAMP));
            }
        };
        appName.setText("播客学伴");appName.setTextColor(0xffffffff);
        appName.setTextSize(getResources().getConfiguration().smallestScreenWidthDp>=600?36:28);
        appName.setTypeface(Typeface.create("Microsoft YaHei",Typeface.BOLD));appName.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams appNameParams=new LinearLayout.LayoutParams(-2,-2);appNameParams.topMargin=dp(18);
        brand.addView(appName,appNameParams);
        TextView slogan=new TextView(this);
        slogan.setText("Live   in the Language");slogan.setTextColor(0xffc084fc);
        slogan.setTextSize(getResources().getConfiguration().smallestScreenWidthDp>=600?24:19);
        slogan.setTypeface(Typeface.create("Microsoft YaHei",Typeface.BOLD));slogan.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams sloganParams=new LinearLayout.LayoutParams(-2,-2);sloganParams.topMargin=dp(12);
        brand.addView(slogan,sloganParams);
        slogan.post(()->{
            int w=slogan.getWidth(), h=slogan.getHeight();
            if(w>0 && h>0){
                android.animation.ValueAnimator anim=android.animation.ValueAnimator.ofInt(0,w);
                anim.setDuration(1200);
                anim.setInterpolator(new android.view.animation.DecelerateInterpolator());
                anim.addUpdateListener(a->{
                    int cw=(int)a.getAnimatedValue();
                    slogan.setClipBounds(new android.graphics.Rect(0,0,cw,h));
                });
                anim.start();
            }
        });
        loading=new ProgressBar(this,null,android.R.attr.progressBarStyleSmall);
        loading.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(0xffc084fc));
        LinearLayout.LayoutParams loadingParams=new LinearLayout.LayoutParams(dp(20),dp(20));loadingParams.topMargin=dp(32);
        brand.addView(loading,loadingParams);
        status = new TextView(this);
        status.setTextColor(0xff94a3b8);status.setTextSize(13);status.setGravity(Gravity.CENTER);
        status.setPadding(dp(24),dp(24),dp(24),dp(12));status.setVisibility(View.GONE);
        brand.addView(status);
        retry = new Button(this);
        retry.setText("重试");retry.setTextColor(0xffe9d5ff);retry.setTextSize(14);retry.setAllCaps(false);
        GradientDrawable retryShape=new GradientDrawable();retryShape.setColor(0xff28173d);retryShape.setCornerRadius(dp(22));retryShape.setStroke(dp(1),0xff68418a);
        retry.setBackground(retryShape);retry.setVisibility(View.GONE);
        retry.setOnClickListener(v -> {status.setVisibility(View.GONE);retry.setVisibility(View.GONE);loading.setVisibility(View.VISIBLE);connect();});
        brand.addView(retry,new LinearLayout.LayoutParams(dp(112),dp(44)));
        FrameLayout.LayoutParams brandParams=new FrameLayout.LayoutParams(-1,-2,Gravity.CENTER);
        brandParams.leftMargin=dp(24);brandParams.rightMargin=dp(24);splash.addView(brand,brandParams);
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE)!=0);
        web.setBackgroundColor(0xff09090f);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setMediaPlaybackRequiresUserGesture(false);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(false);
        web.addJavascriptInterface(new Object(){
            @android.webkit.JavascriptInterface public void shareSubtitle(String title,String text) {
                if(text==null || text.isEmpty() || text.length()>8*1024*1024 || !sharing.compareAndSet(false,true))return;
                new Thread(()->{
                    try {
                        android.content.Intent intent=SubtitleSharing.prepare(MainActivity.this,title,text);
                        runOnUiThread(()->{
                            sharing.set(false);if(destroyed)return;
                            try{startActivity(android.content.Intent.createChooser(intent,"分享字幕到"));}
                            catch(Exception e){web.evaluateJavascript("toast('无法打开系统分享面板','info')",null);}
                        });
                    } catch(Exception e) {
                        sharing.set(false);
                        runOnUiThread(()->{if(!destroyed)web.evaluateJavascript("toast('字幕文稿准备失败，请重试','info')",null);});
                    }
                },"subtitle-share").start();
            }
        },"PodcastAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !request.getUrl().toString().startsWith(server + "/");
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) { pageFailed=true;pageLoaded=false; showError("启动暂未完成，请稍后重试。"); }
            }
            @Override public void onPageFinished(WebView view, String url) {
                showReadyPage(url);
            }
            @Override public void onPageCommitVisible(WebView view,String url){showReadyPage(url);}
        });
        layout.addView(web,new FrameLayout.LayoutParams(-1,-1));
        layout.addView(splash,new FrameLayout.LayoutParams(-1,-1));
        setContentView(layout);
    }

    private void connect() {
        if (destroyed || !connecting.compareAndSet(false,true)) return;
        try {
            android.content.Intent intent = new android.content.Intent(this,BackendService.class);
            if(android.os.Build.VERSION.SDK_INT>=26)startForegroundService(intent);else startService(intent);
        } catch(Exception e) { connecting.set(false); showError("启动暂未完成，请稍后重试。"); return; }
        new Thread(() -> {
            boolean ready=false;
            for(int attempt=0;attempt<30&&!destroyed;attempt++){
                HttpURLConnection connection=null;
                try {
                    connection=(HttpURLConnection)new URL(server+"/api/runtime").openConnection(java.net.Proxy.NO_PROXY);
                    connection.setConnectTimeout(500);connection.setReadTimeout(500);
                    ready=connection.getResponseCode()==200;
                    if(ready)break;
                } catch(Exception ignored){}finally{if(connection!=null)connection.disconnect();}
                try{Thread.sleep(250);}catch(InterruptedException e){break;}
            }
            final boolean available=ready;
            runOnUiThread(() -> {
                connecting.set(false);if(destroyed)return;
                if(!available){showError("启动暂未完成，请稍后重试。");return;}
                status.setVisibility(View.GONE);retry.setVisibility(View.GONE);
                if(!pageLoaded){pageFailed=false;web.loadUrl(server+"/");}
                else web.evaluateJavascript("resumeAndroidPlayback()",null);
            });
        },"backend-health").start();
    }

    @Override protected void onResume(){super.onResume();if(web!=null){web.onResume();connect();}}
    @Override protected void onPause(){
        if(web!=null){web.evaluateJavascript("saveAndroidPlaybackState()",null);web.onPause();}
        super.onPause();
    }
    private int dp(float value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private void showReadyPage(String url){
        if(destroyed || pageFailed || pageLoaded || !url.startsWith(server+"/"))return;
        pageLoaded=true;
        web.evaluateJavascript("(()=>{const ready=()=>{window.androidNativeRuntime=true;document.body.classList.add('android-runtime');if(typeof restoreAndroidPlaybackState==='function')restoreAndroidPlaybackState();};if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ready,{once:true});else ready();})()",null);
        status.setVisibility(View.GONE);retry.setVisibility(View.GONE);
        long remaining=Math.max(0,900-(android.os.SystemClock.uptimeMillis()-splashStarted));
        splash.postDelayed(()->{if(!destroyed && pageLoaded && !pageFailed)splash.animate().alpha(0).setDuration(250).withEndAction(()->splash.setVisibility(View.GONE)).start();},remaining);
    }
    private void showError(String text) {splash.animate().cancel();splash.setAlpha(1);splash.setVisibility(View.VISIBLE);loading.setVisibility(View.GONE);status.setVisibility(View.VISIBLE);status.setText(text);retry.setVisibility(View.VISIBLE);}
    @Override public void onBackPressed() {
        web.evaluateJavascript("(function(){var d=document.getElementById('settingsDialog');if(d&&d.open){closeSettings();return true;}if(document.getElementById('colLeft').classList.contains('collapsed')||document.getElementById('tab-plaza').style.display==='none'){showTab('plaza');expandSidebar();return true;}return false;})()",handled -> {
            if (!destroyed && "false".equals(handled) && getResources().getConfiguration().smallestScreenWidthDp < 600) {
                moveTaskToBack(true);
            }
        });
    }
    @Override protected void onDestroy() { destroyed=true; web.destroy(); super.onDestroy(); }
}
