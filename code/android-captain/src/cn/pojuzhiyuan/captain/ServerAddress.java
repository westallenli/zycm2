package cn.pojuzhiyuan.captain;

import java.net.URI;

/** Only the explicit Wi-Fi IPv4 endpoint is accepted; no DNS or redirects. */
public final class ServerAddress {
    public static String normalize(String input) {
        try {
            String text=input.trim();
            if (!text.contains("://")) text="http://"+text;
            URI u=new URI(text);
            if (!"http".equals(u.getScheme()) || u.getUserInfo()!=null || u.getFragment()!=null) throw new Exception();
            String host=u.getHost();if(host==null)throw new Exception();
            String[] parts=host.split("\\.",-1);if(parts.length!=4)throw new Exception();
            int[] n=new int[4];
            for(int i=0;i<4;i++){
                if(!parts[i].matches("0|[1-9][0-9]{0,2}"))throw new Exception();
                n[i]=Integer.parseInt(parts[i]);if(n[i]>255)throw new Exception();
            }
            boolean local=n[0]==10 || (n[0]==172 && n[1]>=16 && n[1]<=31) || (n[0]==192 && n[1]==168);
            int port=u.getPort();String path=u.getPath();
            if(!local || port<1024 || port>65535 || !(path.isEmpty() || path.equals("/") || path.equals("/mobile"))) throw new Exception();
            return "http://"+host+":"+port;
        } catch(Exception error){throw new IllegalArgumentException("请输入电脑显示的内网地址，例如 192.168.1.37:18705");}
    }
    private ServerAddress(){}
}
