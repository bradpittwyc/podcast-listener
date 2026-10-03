package com.podcastlearner.tablet;

import android.app.Activity;
import android.os.Bundle;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.view.View;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.io.FileInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import org.json.JSONObject;

public class MainActivity extends Activity {
    private WebView web;
    private LinearLayout layout;
    private TextView status;
    private Button retry;
    private final String server = "http://127.0.0.1:8557";
    private final String alias = "podcast-api-keys";
    private NativeBackend backend;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().setStatusBarColor(0xff0d0d18);
        getWindow().setNavigationBarColor(0xff0d0d18);
        layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(0xff0d0d18);
        status = new TextView(this);
        status.setTextColor(0xffc084fc);
        status.setPadding(24,24,24,24);
        layout.addView(status);
        retry = new Button(this);
        retry.setText("重新连接");
        retry.setOnClickListener(v -> connect());
        layout.addView(retry);
        web = new WebView(this);
        web.setBackgroundColor(0xff09090f);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setMediaPlaybackRequiresUserGesture(false);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !request.getUrl().toString().startsWith(server + "/");
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showError("本机服务尚未就绪，请点击重新连接。");
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (url.startsWith(server + "/")) {
                    status.setVisibility(View.GONE);
                    retry.setVisibility(View.GONE);
                }
            }
        });
        layout.addView(web, new LinearLayout.LayoutParams(-1,0,1));
        setContentView(layout);
        connect();
    }

    private byte[] read(InputStream input) throws Exception {
        try (InputStream stream = input; ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096]; int count;
            while ((count = stream.read(buffer)) != -1) bytes.write(buffer,0,count);
            return bytes.toByteArray();
        }
    }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (!store.containsAlias(alias)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(alias,null);
    }

    private String provision() throws Exception {
        File file = new File(getFilesDir(),"provision.json");
        android.content.SharedPreferences preferences = getSharedPreferences("private_keys",MODE_PRIVATE);
        if (file.exists()) {
            byte[] plain = read(new FileInputStream(file));
            new JSONObject(new String(plain,StandardCharsets.UTF_8));
            saveCredentials(new JSONObject(new String(plain,StandardCharsets.UTF_8)));
            java.util.Arrays.fill(plain,(byte)0);
            if (!file.delete()) throw new Exception("Cannot remove provision file");
        }
        if (!preferences.contains("keys")) return null;
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(preferences.getString("iv",""),Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(preferences.getString("keys",""),Base64.NO_WRAP)),StandardCharsets.UTF_8);
    }

    private synchronized void saveCredentials(JSONObject credentials) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE,key());
        String encrypted = Base64.encodeToString(cipher.doFinal(credentials.toString().getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
        if (!getSharedPreferences("private_keys",MODE_PRIVATE).edit().putString("keys",encrypted)
            .putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP)).commit()) throw new Exception("Cannot save credentials");
    }

    private void connect() {
        status.setVisibility(View.VISIBLE); status.setText("正在连接播客服务…"); retry.setVisibility(View.GONE);
        new Thread(() -> {
            try {
                String credentials = provision();
                if (backend == null) {
                    backend = new NativeBackend(getApplicationContext(),credentials == null ? new JSONObject() : new JSONObject(credentials),this::saveCredentials);
                    backend.start();
                }
                runOnUiThread(() -> web.loadUrl(server + "/"));
            } catch (Exception e) {
                runOnUiThread(() -> showError("本机服务启动失败，请重新打开应用。"));
            }
        }).start();
    }

    private void showError(String text) { status.setVisibility(View.VISIBLE); status.setText(text); retry.setVisibility(View.VISIBLE); }
    @Override public void onBackPressed() {
        web.evaluateJavascript("(function(){var d=document.getElementById('settingsDialog');if(d&&d.open){closeSettings();return;}if(document.getElementById('colLeft').classList.contains('collapsed')){expandSidebar();}})()",null);
    }
    @Override protected void onDestroy() { web.destroy(); if (backend != null) backend.close(); super.onDestroy(); }
}
