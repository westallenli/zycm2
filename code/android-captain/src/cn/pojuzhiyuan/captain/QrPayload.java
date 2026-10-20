package cn.pojuzhiyuan.captain;
import java.net.URI;
import java.net.URLDecoder;
import java.util.*;
public final class QrPayload {
    public static String parse(String raw,String base,String event){
        try{
            String value=raw.trim();if(value.matches("qr-[0-9]{1,64}"))return value;
            URI u=new URI(value);if(!ServerAddress.normalize(value).equals(base)||!"/mobile".equals(u.getPath()))throw new Exception();
            Map<String,String> q=new HashMap<>();if(u.getRawQuery()==null)throw new Exception();
            for(String pair:u.getRawQuery().split("&")){String[] kv=pair.split("=",2);if(kv.length!=2)throw new Exception();String k=URLDecoder.decode(kv[0],"UTF-8"),v=URLDecoder.decode(kv[1],"UTF-8");if(q.put(k,v)!=null)throw new Exception();}
            if(q.size()!=2||!event.equals(q.get("event_id"))||!q.getOrDefault("qr_id","").matches("qr-[0-9]{1,64}"))throw new Exception();return q.get("qr_id");
        }catch(Exception e){throw new IllegalArgumentException("此码不是当前事件的到场码，请扫描电脑展示的现场到场二维码。");}
    }
}
