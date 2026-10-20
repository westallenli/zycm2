package cn.pojuzhiyuan.captain;
import android.content.Context;
import android.net.Uri;
import android.graphics.*;
import android.media.ExifInterface;
import java.io.*;

/** Bound decode memory, correct orientation and remove all source metadata. */
public final class PhotoCodec {
    public static final int LIMIT=1024*1024,MAX_EDGE=1600;
    public static byte[] read(Context context,Uri uri)throws Exception {
        File copy=File.createTempFile("field-source-",".tmp",context.getCacheDir());
        try {
            try(InputStream in=context.getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(copy)) {
                if(in==null)throw new IOException("无法读取所选照片");
                byte[] buf=new byte[8192];int n,total=0;
                while((n=in.read(buf))!=-1){total+=n;if(total>40*1024*1024)throw new IOException("原照片超过40MB，请另选照片");out.write(buf,0,n);}
            }
            BitmapFactory.Options o=new BitmapFactory.Options();o.inJustDecodeBounds=true;BitmapFactory.decodeFile(copy.toString(),o);
            if(o.outWidth<=0||o.outHeight<=0)throw new IOException("无法识别照片格式");
            int orientation;try{orientation=new ExifInterface(copy.toString()).getAttributeInt(ExifInterface.TAG_ORIENTATION,1);}catch(IOException ignored){orientation=1;}
            o.inSampleSize=1;while(o.outWidth/o.inSampleSize>MAX_EDGE||o.outHeight/o.inSampleSize>MAX_EDGE)o.inSampleSize*=2;
            o.inJustDecodeBounds=false;o.inPreferredConfig=Bitmap.Config.ARGB_8888;
            Bitmap image=BitmapFactory.decodeFile(copy.toString(),o);if(image==null)throw new IOException("照片读取失败");
            try {
                Matrix m=new Matrix();
                switch(orientation){case 2:m.setScale(-1,1);break;case 3:m.setRotate(180);break;case 4:m.setScale(1,-1);break;case 5:m.setRotate(90);m.postScale(-1,1);break;case 6:m.setRotate(90);break;case 7:m.setRotate(270);m.postScale(-1,1);break;case 8:m.setRotate(270);break;}
                if(!m.isIdentity()){Bitmap rotated=Bitmap.createBitmap(image,0,0,image.getWidth(),image.getHeight(),m,true);if(rotated!=image){image.recycle();image=rotated;}}
                float ratio=Math.min(1f,(float)MAX_EDGE/Math.max(image.getWidth(),image.getHeight()));
                if(ratio<1){Bitmap scaled=Bitmap.createScaledBitmap(image,Math.max(1,Math.round(image.getWidth()*ratio)),Math.max(1,Math.round(image.getHeight()*ratio)),true);image.recycle();image=scaled;}
                ByteArrayOutputStream out=new ByteArrayOutputStream();
                for(int quality=85;quality>=45;quality-=10){out.reset();if(!image.compress(Bitmap.CompressFormat.JPEG,quality,out))throw new IOException("压缩失败");if(out.size()<=LIMIT)return out.toByteArray();}
                throw new IOException("照片压缩后仍超过1MB，请另选照片");
            } finally {image.recycle();}
        } finally {copy.delete();}
    }
    private PhotoCodec(){}
}
