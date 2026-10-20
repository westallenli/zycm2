package cn.pojuzhiyuan.captain;
public final class MapMath {
    public static double x(double lon,int z){return (lon+180)/360*(256L<<z);}
    public static double y(double lat,int z){double r=Math.toRadians(Math.max(-85.05112878,Math.min(85.05112878,lat)));return (1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*(256L<<z);}
    public static double lon(double x,int z){return x/(256L<<z)*360-180;}
    public static double lat(double y,int z){return Math.toDegrees(Math.atan(Math.sinh(Math.PI*(1-2*y/(256L<<z)))));}
}
