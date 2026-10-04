package com.podcastlearner.tablet;

import android.content.Context;
import android.content.Intent;
import android.content.ClipData;
import android.net.Uri;
import androidx.core.content.FileProvider;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

final class SubtitleSharing {
    static String filename(String title) {
        String name=(title==null?"":title).replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]","_").trim();
        if(name.length()>100)name=name.substring(0,100);
        if(name.isEmpty() || name.equals(".") || name.equals(".."))name="播客字幕";
        return name+".txt";
    }
    static Intent prepare(Context context,String title,String text) throws Exception {
        File root=new File(context.getCacheDir(),"shared_subtitles");
        if(!root.isDirectory() && !root.mkdirs())throw new java.io.IOException("Cannot create subtitle share cache");
        File[] old=root.listFiles();long cutoff=System.currentTimeMillis()-7L*24*60*60*1000;
        if(old!=null)for(File file:old)if(file.isFile()&&file.lastModified()<cutoff)file.delete();
        File file=new File(root,UUID.randomUUID().toString().substring(0,8)+"-"+filename(title));
        try(java.io.OutputStream output=new java.io.FileOutputStream(file)){output.write(text.getBytes(StandardCharsets.UTF_8));}
        Uri uri=FileProvider.getUriForFile(context,context.getPackageName()+".subtitles",file,filename(title));
        Intent intent=new Intent(Intent.ACTION_SEND);
        intent.setType("text/plain");
        intent.putExtra(Intent.EXTRA_TITLE,title==null?"播客字幕":title);
        intent.putExtra(Intent.EXTRA_SUBJECT,title==null?"播客字幕":title);
        // Send only the document URI, never transcript text through Binder or
        // a receiving app's input field. The file contains the full transcript.
        intent.putExtra(Intent.EXTRA_STREAM,uri);
        intent.setClipData(ClipData.newUri(context.getContentResolver(),"字幕文稿",uri));
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return intent;
    }
}
