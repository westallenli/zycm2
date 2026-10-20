package cn.pojuzhiyuan.captain;
import android.content.*;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import java.io.*;

/** Only a randomly named temporary capture file is shared with the camera. */
public final class CaptureProvider extends ContentProvider {
    public boolean onCreate(){return true;}
    private File target(Uri uri)throws FileNotFoundException {
        String name=uri.getLastPathSegment();
        if(!"cn.pojuzhiyuan.captain.capture".equals(uri.getAuthority())||uri.getPathSegments().size()!=1||name==null||!name.matches("capture-[a-f0-9-]{36}\\.jpg"))throw new FileNotFoundException();
        File root=new File(getContext().getCacheDir(),"captures"),file=new File(root,name);
        if(!file.exists())throw new FileNotFoundException();return file;
    }
    public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException {
        if(!mode.equals("w")&&!mode.equals("r")&&!mode.equals("rw"))throw new FileNotFoundException();
        return ParcelFileDescriptor.open(target(uri),mode.equals("r")?ParcelFileDescriptor.MODE_READ_ONLY:ParcelFileDescriptor.MODE_READ_WRITE|(mode.equals("w")?ParcelFileDescriptor.MODE_TRUNCATE:0));
    }
    public String getType(Uri uri){return "image/jpeg";}
    public Cursor query(Uri u,String[] p,String s,String[] a,String o){return null;}
    public Uri insert(Uri u,ContentValues v){throw new UnsupportedOperationException();}
    public int update(Uri u,ContentValues v,String s,String[] a){throw new UnsupportedOperationException();}
    public int delete(Uri u,String s,String[] a){throw new UnsupportedOperationException();}
}
