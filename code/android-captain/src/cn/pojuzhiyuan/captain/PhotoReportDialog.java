package cn.pojuzhiyuan.captain;
import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.*;
import android.net.Uri;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;

/** One explicit field report; uncertain submissions retain the same request ID. */
public final class PhotoReportDialog {
    public interface Host{void submit(JSONObject value,Done done);}
    public interface Done{void finish(boolean ok);}
    private final Activity activity;private final Host host;private final String team;private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private final ArrayList<byte[]> photos=new ArrayList<>();private final ArrayList<Bitmap> thumbnails=new ArrayList<>();
    private final ArrayList<View> controls=new ArrayList<>();private AlertDialog dialog;private EditText text;private LinearLayout gallery;private TextView hint;private String request=UUID.randomUUID().toString();private boolean busy=false,attempted=false,closed=false;private Uri capture;private File captureFile;
    public PhotoReportDialog(Activity a,String team,Host h){activity=a;this.team=team;host=h;}
    public boolean isOpen(){return !closed;}
    private int dp(int n){return(int)(n*activity.getResources().getDisplayMetrics().density+.5f);}
    private Button button(String name,Runnable action){Button b=new Button(activity);b.setText(name);b.setAllCaps(false);b.setMinHeight(dp(48));b.setOnClickListener(v->{if(!busy)action.run();});controls.add(b);return b;}
    public void show(){
        LinearLayout box=new LinearLayout(activity);box.setOrientation(1);box.setPadding(dp(16),dp(4),dp(16),dp(8));
        text=new EditText(activity);text.setHint("填写现场情况");text.setSingleLine(false);text.setMinLines(3);text.setMaxLines(6);text.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(2000)});box.addView(text);controls.add(text);
        LinearLayout actions=new LinearLayout(activity);actions.addView(button("拍照",this::take),new LinearLayout.LayoutParams(0,dp(48),1));actions.addView(button("选照片",this::pick),new LinearLayout.LayoutParams(0,dp(48),1));box.addView(actions);
        gallery=new LinearLayout(activity);gallery.setOrientation(1);box.addView(gallery);hint=new TextView(activity);hint.setText("照片可选，最多3张；上传前自动压缩至每张1MB以内。");box.addView(hint);
        ScrollView scroll=new ScrollView(activity);scroll.addView(box);
        dialog=new AlertDialog.Builder(activity).setTitle("信息上报").setView(scroll).setNegativeButton("取消",null).setPositiveButton("提交",null).create();
        dialog.setOnShowListener(d->{Button send=dialog.getButton(-1);controls.add(send);controls.add(dialog.getButton(-2));send.setOnClickListener(v->send());});
        dialog.setOnDismissListener(d->dispose());dialog.show();
    }
    private void setBusy(boolean value,String notice){busy=value;for(View v:controls)v.setEnabled(!value);dialog.setCancelable(!value);hint.setText(notice);}
    private void changed(){if(attempted){request=UUID.randomUUID().toString();attempted=false;}}
    private boolean room(){if(photos.size()<3)return true;hint.setText("最多3张，可先移除照片。");return false;}
    private void pick(){if(!room())return;try{activity.startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE),83);}catch(ActivityNotFoundException e){hint.setText("手机没有可用的照片选择器。");}}
    private void take(){if(!room())return;if(activity.checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED){activity.requestPermissions(new String[]{Manifest.permission.CAMERA},84);return;}try{
        File dir=new File(activity.getCacheDir(),"captures");dir.mkdirs();clearCapture();captureFile=new File(dir,"capture-"+UUID.randomUUID()+".jpg");if(!captureFile.createNewFile())throw new IOException();
        capture=Uri.parse("content://cn.pojuzhiyuan.captain.capture/"+captureFile.getName());
        Intent camera=new Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT,capture).addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION|Intent.FLAG_GRANT_READ_URI_PERMISSION);camera.setClipData(ClipData.newRawUri("上报照片",capture));activity.startActivityForResult(camera,82);
    }catch(Exception e){clearCapture();hint.setText("无法打开相机，可改用选照片。");}}
    public void permission(int code,int[] grants){if(closed)return;if(code==84){if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED)take();else hint.setText("相机权限未开启，仍可选照片或仅文字上报。");}}
    public boolean result(int code,int result,Intent data){if(closed||code!=82&&code!=83)return false;Uri uri=code==82?capture:data==null?null:data.getData();if(result!=Activity.RESULT_OK||uri==null){if(code==82)clearCapture();return true;}setBusy(true,"正在压缩照片…");worker.execute(()->{
        byte[] bytes=null;String failure=null;try{bytes=PhotoCodec.read(activity,uri);}catch(Exception e){failure=e.getMessage();}catch(OutOfMemoryError e){failure="照片过大，请换一张较小照片";}finally{if(code==82)clearCapture();}
        final byte[] ready=bytes;final String error=failure;activity.runOnUiThread(()->{if(closed)return;setBusy(false,error==null?"照片已压缩，可预览、移除或继续添加。":error);if(ready!=null){changed();photos.add(ready);renderPhotos();}});
    });return true;}
    private void renderPhotos(){gallery.removeAllViews();for(Bitmap b:thumbnails)b.recycle();thumbnails.clear();for(int i=0;i<photos.size();i++){final int index=i;byte[] bytes=photos.get(i);BitmapFactory.Options o=new BitmapFactory.Options();o.inSampleSize=4;Bitmap bitmap=BitmapFactory.decodeByteArray(bytes,0,bytes.length,o);thumbnails.add(bitmap);LinearLayout row=new LinearLayout(activity);row.setGravity(Gravity.CENTER_VERTICAL);ImageView image=new ImageView(activity);image.setImageBitmap(bitmap);image.setScaleType(ImageView.ScaleType.CENTER_CROP);image.setContentDescription("上报照片"+(i+1));image.setOnClickListener(v->preview(bytes));row.addView(image,new LinearLayout.LayoutParams(dp(80),dp(64)));TextView label=new TextView(activity);label.setText("  照片"+(i+1)+" · "+Math.max(1,bytes.length/1024)+"KB");row.addView(label,new LinearLayout.LayoutParams(0,-2,1));row.addView(button("移除",()->{changed();photos.remove(index);renderPhotos();}),new LinearLayout.LayoutParams(dp(72),dp(48)));gallery.addView(row);}}
    private void preview(byte[] bytes){Bitmap b=BitmapFactory.decodeByteArray(bytes,0,bytes.length);ImageView image=new ImageView(activity);image.setImageBitmap(b);image.setAdjustViewBounds(true);new AlertDialog.Builder(activity).setView(image).setPositiveButton("关闭",null).setOnDismissListener(d->b.recycle()).show();}
    private String lastContent="";
    private void send(){if(busy)return;String content=text.getText().toString().trim();if(content.isEmpty()){text.setError("请填写现场情况");return;}try{if(attempted&&!content.equals(lastContent))changed();lastContent=content;JSONArray encoded=new JSONArray();for(byte[] photo:photos)encoded.put(Base64.encodeToString(photo,Base64.NO_WRAP));JSONObject value=new JSONObject().put("team_id",team).put("content",content).put("request_id",request).put("photos",encoded);attempted=true;setBusy(true,"正在上传，请稍候…");host.submit(value,ok->{if(closed)return;if(ok)dialog.dismiss();else setBusy(false,"提交未确认，内容和照片已保留。可原样重试，同一条提交不会重复登记。");});}catch(Exception e){setBusy(false,"未提交："+e.getMessage());}}
    private void clearCapture(){if(capture!=null)activity.revokeUriPermission(capture,Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_WRITE_URI_PERMISSION);if(captureFile!=null)captureFile.delete();capture=null;captureFile=null;}
    public void dispose(){if(closed)return;closed=true;worker.shutdownNow();clearCapture();for(Bitmap b:thumbnails)b.recycle();photos.clear();}
}
