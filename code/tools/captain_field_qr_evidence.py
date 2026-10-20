"""Decode both delivered SVG symbols with the same verified QR library."""
from pathlib import Path
import json,re,subprocess
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/队长现场App证据_20261018'
J=next((N/'.android-tools/jdk').glob('*/bin'));jar=W/'android-captain/libs/zxing-core-3.5.3.jar'
src=E/'DecodeFieldQr.java'
src.write_text('''import java.nio.file.*;import com.google.zxing.*;import com.google.zxing.common.*;
class DecodeFieldQr {public static void main(String[] a)throws Exception{int n=Integer.parseInt(a[1]);byte[] f=Files.readAllBytes(Paths.get(a[0]));System.out.println(new MultiFormatReader().decode(new BinaryBitmap(new HybridBinarizer(new PlanarYUVLuminanceSource(f,n,n,0,0,n,n,false)))).getText());}}
''',encoding='utf-8')
subprocess.run([J/'javac.exe','-classpath',jar,'-d',E,src],check=True,capture_output=True)
expected=['http://192.168.1.37:18705/app/download','http://192.168.1.37:18705/mobile?event_id=fire-001&qr_id=qr-001'];result=[]
symbols=re.findall(r'<svg\b.*?</svg>',(E/'installation-page.html').read_text(encoding='utf-8'),re.S);assert len(symbols)==2
for i,svg in enumerate(symbols):
    (E/f'delivery-qr-{i}.svg').write_text(svg,encoding='utf-8');n=int(re.search(r'viewBox="0 0 (\d+) ',svg)[1]);scale=8;size=n*scale;frame=bytearray([255])*(size*size)
    for x,y in re.findall(r'M(\d+),(\d+)h1v1h-1z',svg):
        for row in range(int(y)*scale,(int(y)+1)*scale):frame[row*size+int(x)*scale:row*size+(int(x)+1)*scale]=bytes(scale)
    raw=E/f'delivery-qr-{i}.gray';raw.write_bytes(frame)
    p=subprocess.run([J/'java.exe','-classpath',str(E)+';'+str(jar),'DecodeFieldQr',raw,str(size)],capture_output=True,text=True,check=True)
    value=p.stdout.strip();assert value==expected[i],value;result.append(dict(symbol=i,payload=value,decoded=True))
(E/'qr-decode.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(result)
