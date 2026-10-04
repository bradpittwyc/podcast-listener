package com.podcastlearner.tablet;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

/** Explicit cancellation, including a cancel request arriving before job startup. */
final class TranscriptionJobs {
    static final class Control {
        final AtomicBoolean stopped = new AtomicBoolean();
        private final Set<Runnable> callbacks = Collections.newSetFromMap(new ConcurrentHashMap<Runnable,Boolean>());
        Runnable onCancel(Runnable callback) {
            callbacks.add(callback);
            if(stopped.get() && callbacks.remove(callback))callback.run();
            return ()->callbacks.remove(callback);
        }
        void stop() {
            stopped.set(true);
            for(Runnable callback:callbacks)if(callbacks.remove(callback))try{callback.run();}catch(Exception ignored){}
        }
    }
    private final Map<String,Control> active = new ConcurrentHashMap<>();
    private final LinkedHashMap<String,Long> cancelled = new LinkedHashMap<>();
    static String token(String value) {
        if(value==null || value.isEmpty())return UUID.randomUUID().toString();
        if(!value.matches("[A-Za-z0-9_-]{1,100}"))throw new IllegalArgumentException("Invalid transcription request ID");
        return value;
    }
    Control register(String token) {
        Control control=new Control(),previous;
        synchronized(this){
            previous=active.put(token,control);
            Long when=cancelled.get(token);
            if(when!=null && System.nanoTime()-when<java.util.concurrent.TimeUnit.MINUTES.toNanos(5))control.stop();
        }
        if(previous!=null)previous.stop();
        return control;
    }
    Control get(String token){return active.get(token);}
    void cancel(String token) {
        Control control;
        synchronized(this){
            cancelled.remove(token);cancelled.put(token,System.nanoTime());
            while(cancelled.size()>256)cancelled.remove(cancelled.keySet().iterator().next());
            control=active.get(token);
        }
        if(control!=null)control.stop();
    }
    synchronized void release(String token,Control control){control.stop();if(active.get(token)==control)active.remove(token);}
    void close(){for(Control control:active.values())control.stop();active.clear();}
}
