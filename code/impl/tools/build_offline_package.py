"""Prepare a declared local raster package, without downloading or authorizing it.

python tools/build_offline_package.py LOCAL_INPUT_FOLDER NEW_OUTPUT.zip
Input contains manifest.json plus tiles/z/x/y.png|jpg|webp or map.mbtiles.
The named event deputy must still import and confirm the reported license.
"""
from pathlib import Path
import argparse,base64,io,sys,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panda_brain.offline_resources import archive,MAX_CONTENT,MAX_ARCHIVE,MAX_TILES

def build(folder,output):
    root=Path(folder).resolve(strict=True);target=Path(output).resolve()
    if not root.is_dir() or target.exists():raise ValueError('输入须为本地目录，输出须为新文件，不覆盖原包。')
    stream=io.BytesIO();size=0;count=0
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob('*')):
            if p.is_symlink():raise ValueError('不接收链接资源。')
            if not p.is_file():continue
            if not p.resolve().is_relative_to(root):raise ValueError('资源路径越界。')
            size+=p.stat().st_size;count+=1
            if size>MAX_CONTENT or count>MAX_TILES+1:raise ValueError('资源超过大小或数量边界。')
            z.writestr(p.relative_to(root).as_posix(),p.read_bytes())
    raw=stream.getvalue()
    if len(raw)>MAX_ARCHIVE:raise ValueError('压缩包超过16MiB。')
    archive(base64.b64encode(raw).decode())
    with target.open('xb') as f:f.write(raw)
    return target

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='打包本地已声明栅格资料，不联网、不替代使用授权。')
    parser.add_argument('input');parser.add_argument('output');args=parser.parse_args()
    print(build(args.input,args.output))
