package com.podcastlearner.tablet;

import java.io.File;
import java.util.*;

/** Trim only episode caches, preserving active jobs, credentials and exported notes. */
final class CachePolicy {
    static int prune(File folder,Set<String> protectedKeys,long budget,long maxAge) {
        File[] files=folder.listFiles();if(files==null)return 0;
        Map<String,List<File>> groups=new HashMap<>();long total=0;
        for(File file:files){
            String name=file.getName();if(!file.isFile()||!name.matches("[0-9a-f]{64}-aliyun-sentences-v2\\.json.*"))continue;
            String key=name.substring(0,64);List<File> group=groups.get(key);
            if(group==null){group=new ArrayList<>();groups.put(key,group);}group.add(file);total+=file.length();
        }
        List<String> keys=new ArrayList<>(groups.keySet());
        Collections.sort(keys,(a,b)->Long.compare(newest(groups.get(a)),newest(groups.get(b))));
        int removed=0;long cutoff=System.currentTimeMillis()-maxAge;
        for(String key:keys){
            if(protectedKeys.contains(key)||(total<=budget&&newest(groups.get(key))>=cutoff))continue;
            for(File file:groups.get(key)){long size=file.length();if(file.delete()){total-=size;removed++;}}
        }
        return removed;
    }
    private static long newest(List<File> files){long latest=0;for(File file:files)latest=Math.max(latest,file.lastModified());return latest;}
}
