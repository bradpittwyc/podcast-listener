package com.podcastlearner.tablet;

import android.content.Context;
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

final class CredentialStore {
    private final Context context;
    private final String alias = "podcast-api-keys";
    CredentialStore(Context context) { this.context = context.getApplicationContext(); }
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

    JSONObject load() throws Exception {
        File file = new File(context.getFilesDir(),"provision.json");
        android.content.SharedPreferences preferences = context.getSharedPreferences("private_keys",Context.MODE_PRIVATE);
        JSONObject credentials=new JSONObject();
        if(preferences.contains("keys")) {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(preferences.getString("iv",""),Base64.NO_WRAP)));
            credentials=new JSONObject(new String(cipher.doFinal(Base64.decode(preferences.getString("keys",""),Base64.NO_WRAP)),StandardCharsets.UTF_8));
        }
        if (file.exists()) {
            byte[] plain = read(new FileInputStream(file));
            try {
                JSONObject incoming=new JSONObject(new String(plain,StandardCharsets.UTF_8));
                java.util.Iterator<String> names=incoming.keys();
                while(names.hasNext()){String name=names.next();credentials.put(name,incoming.get(name));}
                saveCredentials(credentials);
            } finally {java.util.Arrays.fill(plain,(byte)0);}
            if (!file.delete()) throw new Exception("Cannot remove provision file");
        }
        return credentials;
    }

    synchronized void saveCredentials(JSONObject credentials) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE,key());
        String encrypted = Base64.encodeToString(cipher.doFinal(credentials.toString().getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
        if (!context.getSharedPreferences("private_keys",Context.MODE_PRIVATE).edit().putString("keys",encrypted)
            .putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP)).commit()) throw new Exception("Cannot save credentials");
    }

}
