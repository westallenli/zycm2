package cn.pojuzhiyuan.captain;
import android.app.Activity;
import android.os.Bundle;
import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.hardware.Camera;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.view.*;
import android.widget.*;
import com.google.zxing.*;
import com.google.zxing.common.HybridBinarizer;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Decodes preview frames locally. Images are never saved or uploaded. */
public final class ScanActivity extends Activity implements SurfaceHolder.Callback {
    private Camera camera;private SurfaceView surface;private TextView hint;private boolean ready=false,active=false,finished=false;
    private final ExecutorService decoder=Executors.newSingleThreadExecutor();private final AtomicBoolean reading=new AtomicBoolean(false);private long last=0;
    @Override public void onCreate(Bundle state){super.onCreate(state);LinearLayout box=new LinearLayout(this);box.setOrientation(1);box.setBackgroundColor(Color.BLACK);hint=new TextView(this);hint.setText("对准电脑显示的现场到场码\n识别后确认本队按满编到场");hint.setTextColor(Color.WHITE);hint.setTextSize(18);hint.setPadding(24,48,24,24);box.addView(hint);surface=new SurfaceView(this);surface.getHolder().addCallback(this);box.addView(surface,new LinearLayout.LayoutParams(-1,0,1));Button cancel=new Button(this);cancel.setText("返回");cancel.setOnClickListener(v->finish());box.addView(cancel);setContentView(box);if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.CAMERA},71);}
    @Override protected void onResume(){super.onResume();active=true;open();}
    @Override protected void onPause(){active=false;release();super.onPause();}
    @Override protected void onDestroy(){decoder.shutdownNow();super.onDestroy();}
    @Override public void surfaceCreated(SurfaceHolder h){ready=true;open();}
    @Override public void surfaceChanged(SurfaceHolder h,int format,int width,int height){}
    @Override public void surfaceDestroyed(SurfaceHolder h){ready=false;release();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants){super.onRequestPermissionsResult(request,permissions,grants);if(request==71){if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED)open();else hint.setText("未获得相机权限。请返回，允许相机权限后再扫描到场码。");}}
    private void open(){
        if(!ready||!active||finished||camera!=null||checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED)return;
        try{int id=0;Camera.CameraInfo info=new Camera.CameraInfo();for(int i=0;i<Camera.getNumberOfCameras();i++){Camera.getCameraInfo(i,info);if(info.facing==Camera.CameraInfo.CAMERA_FACING_BACK){id=i;break;}}Camera.getCameraInfo(id,info);camera=Camera.open(id);Camera.Parameters p=camera.getParameters();Camera.Size best=p.getSupportedPreviewSizes().get(0);for(Camera.Size s:p.getSupportedPreviewSizes())if(Math.abs(s.width-640)+Math.abs(s.height-480)<Math.abs(best.width-640)+Math.abs(best.height-480))best=s;p.setPreviewSize(best.width,best.height);p.setPreviewFormat(ImageFormat.NV21);List<String> modes=p.getSupportedFocusModes();if(modes!=null&&modes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE))p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);camera.setParameters(p);int rotation=getWindowManager().getDefaultDisplay().getRotation()*90;camera.setDisplayOrientation((info.orientation-rotation+360)%360);camera.setPreviewDisplay(surface.getHolder());camera.setPreviewCallback(this::frame);camera.startPreview();}
        catch(Exception e){release();hint.setText("相机暂不可用。请返回并检查相机权限或占用情况后重试。");}
    }
    private void frame(byte[] data,Camera source){
        if(finished||!active||System.currentTimeMillis()-last<400||!reading.compareAndSet(false,true))return;last=System.currentTimeMillis();final byte[] copy=data.clone();final Camera.Size size=source.getParameters().getPreviewSize();
        decoder.execute(()->{try{Map<DecodeHintType,Object> hints=new EnumMap<>(DecodeHintType.class);hints.put(DecodeHintType.POSSIBLE_FORMATS,Collections.singletonList(BarcodeFormat.QR_CODE));hints.put(DecodeHintType.TRY_HARDER,Boolean.TRUE);MultiFormatReader reader=new MultiFormatReader();Result result=reader.decode(new BinaryBitmap(new HybridBinarizer(new PlanarYUVLuminanceSource(copy,size.width,size.height,0,0,size.width,size.height,false))),hints);runOnUiThread(()->{if(!active||finished)return;finished=true;setResult(RESULT_OK,new Intent().putExtra("qr",result.getText()));finish();});}catch(Exception ignored){}finally{reading.set(false);}});
    }
    private void release(){if(camera!=null){try{camera.setPreviewCallback(null);camera.stopPreview();camera.release();}catch(RuntimeException ignored){}camera=null;}}
}
