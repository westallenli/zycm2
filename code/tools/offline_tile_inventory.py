"""Read-only tile index census; availability is not certified ground resolution."""
from pathlib import Path
import collections,csv,json,math,sqlite3,sys,html

def position(x,y,z):
    n=1<<z
    return x/n*360-180,math.degrees(math.atan(math.sinh(math.pi*(1-2*y/n))))

def approximate_name(lon,lat):
    regions=[
      ('陕西·西安南郊与秦岭局部',108.2,33.15,109.5,34.75),
      ('陕西·黄陵附近',108.3,35.25,109.5,36.3),
      ('陕西·铜川附近',108.3,34.75,109.6,35.25),
      ('山东·烟台附近',121,37.2,121.8,37.9),
      ('山东·寿光附近',118.9,36.8,119.6,37.4),
      ('山东·胶州附近',119.65,36,120.4,36.65),
      ('安徽·合肥北部附近',116.6,31.65,117.85,32.2),
      ('安徽·滁州附近',118.1,32.2,118.6,32.6),
      ('广西·桂林南部附近',110,24.85,110.65,25.6),
      ('广西·北海附近',108.9,20.9,109.45,21.35),
      ('广东·廉江附近',109.5,21.2,110.1,21.9),
      ('浙江·象山附近',121.6,29.1,122.3,29.8),
      ('宁夏·青铜峡附近',105.6,38.1,106.2,38.55),
      ('新疆·喀什附近',75.6,39.1,76.4,40),
      ('新疆·库尔勒附近',85.8,41.1,86.5,42.25),
      ('日本·东京附近',139,35,140.5,36.5),
      ('新加坡附近',103.3,.8,104.5,1.8),
      ('南美洲·哥伦比亚附近',-74,1,-71,4),
      ('南美洲·圭亚那附近',-59,0,-56,4),
      ('南美洲·巴西局部',-43,-17,-40,-13),
    ]
    for name,w,s,e,n in regions:
        if w<=lon<=e and s<=lat<=n:return name
    return '地名待核（以经纬度为准）'

def census(path,out):
    path=Path(path).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
    levels=collections.defaultdict(set)
    for (key,) in db.execute('SELECT key FROM tiles'):
        for z in range(23):
            n=1<<z
            if z*n*n<=key<(z+1)*n*n:levels[z].add(((key>>z)%n,key%n));break
    db.close();rows=[];regions={}
    for z in (16,17,18,19,20):
        remaining=set(levels[z]);groups=[]
        while remaining:
            seed=remaining.pop();stack=[seed];group={seed}
            while stack:
                x,y=stack.pop()
                for p in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                    if p in remaining:remaining.remove(p);stack.append(p);group.add(p)
            groups.append(group)
        groups.sort(key=lambda g:(-len(g),min(g)))
        for number,g in enumerate(groups,1):
            minx=min(x for x,y in g);maxx=max(x for x,y in g);miny=min(y for x,y in g);maxy=max(y for x,y in g)
            # Representative point is an actual tile, never an empty bbox centre.
            cx=(minx+maxx)/2;cy=(miny+maxy)/2
            x,y=min(g,key=lambda p:((p[0]-cx)**2+(p[1]-cy)**2,p))
            lon,lat=position(x+.5,y+.5,z);name=approximate_name(lon,lat)
            west,north=position(minx,miny,z);east,south=position(maxx+1,maxy+1,z)
            side=40075.016686*math.cos(math.radians(lat))/(1<<z)
            row=dict(level=z,component=number,approximate_region=name,tiles=len(g),
                representative_lon=round(lon,6),representative_lat=round(lat,6),
                west=round(west,6),south=round(south,6),east=round(east,6),north=round(north,6),
                bbox_fill_percent=round(100*len(g)/((maxx-minx+1)*(maxy-miny+1)),1),
                available_area_km2_approx=round(len(g)*side*side,3))
            rows.append(row)
            summary=regions.setdefault(name,dict(name=name,levels={}))
            summary['levels'][z]=summary['levels'].get(z,0)+len(g)
    proof=dict(file=str(path),bytes=path.stat().st_size,total_tiles=sum(map(len,levels.values())),
        level_counts={z:len(v) for z,v in sorted(levels.items())},
        definition='16–20级原生瓦片索引可用性；并未认证原始影像分辨率、时间或定位误差。四邻接片区可有内部缺口。',
        place_labels='地名由经纬度粗定位，不用于行政归属判定；地名待核行保留精确坐标。',
        regions=sorted(regions.values(),key=lambda r:-sum(r['levels'].values())),components=rows)
    (out/'offline-tile-inventory.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'离线高清瓦片全部片区.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    lines=['# 本地离线高层级瓦片覆盖清单','',
      '数据库只读盘点。高清在此指16–20级瓦片索引存在，不等于影像达到某个米级分辨率，也不证明拍摄日期或定位精度。',
      '地名按经纬度粗定位，不是行政边界判定。片区外接范围内可能有缺图，不能把范围框当作全覆盖。',
      '',f'总计{proof["total_tiles"]:,}张瓦片；高层级四邻接片区记录{len(rows)}条。',
      '', '| 粗定位区域 | 16级 | 17级 | 18级 | 19级 | 20级 |','|---|---:|---:|---:|---:|---:|']
    for r in proof['regions']:lines.append('|'+r['name']+'|'+'|'.join(str(r['levels'].get(z,0)) for z in range(16,21))+'|')
    lines+=['','## 16级主要连续片区','', '| 区域 | 瓦片数 | 代表坐标（经度，纬度） | 框内填充率 |','|---|---:|---|---:|']
    for r in [r for r in rows if r['level']==16][:35]:
        lines.append(f'|{r["approximate_region"]}|{r["tiles"]}|{r["representative_lon"]}, {r["representative_lat"]}|{r["bbox_fill_percent"]}%|')
    lines+=['','完整片区边界、逐层计数和所有小片区见同目录CSV和JSON。',
      '不能依据卫星图上的山路或空地认定消防车辆可通行；拟迁移演示仍为虚构。']
    (out/'本地离线高清地图覆盖清单.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Searchable standalone report, entirely local; no map CDN or remote requests.
    head='<html lang="zh-CN"><meta charset="utf-8"><title>本地离线地图覆盖清单</title><style>body{font:16px system-ui;margin:32px;color:#203329}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ccd6cf;text-align:left}input{padding:10px;width:420px;max-width:90%}thead{background:#eef3ef}small{color:#52665a}</style><h1>本地离线高层级瓦片覆盖</h1><p>16–20级瓦片存在，不代表米级分辨率或专业定位认证。地名为粗定位；四邻接片区可能有内部缺口。</p><input id="q" placeholder="搜索区域、层级或经纬度"><p><small>全部片区均来自本机SQLite索引；本页无需联网。</small></p><table><thead><tr><th>区域（近似）</th><th>层级</th><th>瓦片数</th><th>代表坐标：经度，纬度</th><th>框内填充率</th></tr></thead><tbody>'
    body=''.join(f'<tr><td>{html.escape(r["approximate_region"])}</td><td>{r["level"]}</td><td>{r["tiles"]}</td><td>{r["representative_lon"]}, {r["representative_lat"]}</td><td>{r["bbox_fill_percent"]}%</td></tr>' for r in rows)
    tail='</tbody></table><script>document.getElementById("q").addEventListener("input",e=>{let q=e.target.value.trim().toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))})</script></html>'
    (out/'本地离线地图覆盖清单.html').write_text(head+body+tail,encoding='utf-8')
    print(json.dumps(dict(total_tiles=proof['total_tiles'],components=len(rows),regions=proof['regions']),ensure_ascii=False))

if __name__=='__main__':census(sys.argv[1],sys.argv[2])
