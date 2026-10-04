package com.podcastlearner.tablet;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import org.json.JSONObject;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Own the local server independently of any Activity or WebView instance. */
public final class BackendService extends Service {
    private final ExecutorService startup = Executors.newSingleThreadExecutor();
    private NativeBackend backend;
    private boolean starting, closing;

    @Override public void onCreate() {
        super.onCreate();
        String channel="podcast-background";
        if(Build.VERSION.SDK_INT>=26){
            NotificationChannel item=new NotificationChannel(channel,"播客播放与字幕",NotificationManager.IMPORTANCE_LOW);
            item.setDescription("保持本机播放和字幕服务连接");
            getSystemService(NotificationManager.class).createNotificationChannel(item);
        }
        Intent open=new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP|Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent content=PendingIntent.getActivity(this,0,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder=Build.VERSION.SDK_INT>=26?new Notification.Builder(this,channel):new Notification.Builder(this);
        startForeground(8557,builder.setSmallIcon(com.podcastlearner.tablet.R.drawable.ic_podcast)
            .setContentTitle("播客学伴").setContentText("播放与字幕服务运行中，点击返回")
            .setContentIntent(content).setOngoing(true).setCategory(Notification.CATEGORY_SERVICE).build());
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        synchronized(this){
            if(closing||backend!=null||starting)return START_STICKY;
            starting=true;
        }
        startup.execute(()->{
            NativeBackend candidate=null;
            try{
                CredentialStore store=new CredentialStore(getApplicationContext());
                JSONObject keys=store.load();
                candidate=new NativeBackend(getApplicationContext(),keys,store::saveCredentials);
                candidate.start();
                synchronized(this){if(closing)candidate.close();else backend=candidate;starting=false;}
            }catch(Exception ignored){
                if(candidate!=null)candidate.close();
                synchronized(this){starting=false;}
            }
        });
        return START_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return null;}
    @Override public void onDestroy(){
        synchronized(this){closing=true;if(backend!=null){backend.close();backend=null;}}
        startup.shutdownNow();super.onDestroy();
    }
}
