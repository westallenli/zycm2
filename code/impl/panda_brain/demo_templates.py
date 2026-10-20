"""Fixed, explicitly simulated templates; never relocate recorded audit history."""
from copy import deepcopy
import math

QINLING='qinling-north-v1'
ANCHOR=(33.945638,108.778381)
NOTICE='秦岭真实影像上的虚构演练；火线、通道、部署及保障点未按实地地形核验。'

def relocate(lat,lon,template_id='qinglan'):
    if template_id=='qinglan':return lat,lon
    if template_id!=QINLING:raise ValueError('未知的模拟场景模板。')
    from .initial_map import bundle
    a,b=bundle()['anchor'];scale=math.cos(math.radians(a))/math.cos(math.radians(ANCHOR[0]))
    return round(ANCHOR[0]+lat-a,6),round(ANCHOR[1]+(lon-b)*scale,6)

def label(value,template_id='qinglan'):
    if template_id=='qinglan':return value
    if template_id!=QINLING:raise ValueError('未知的模拟场景模板。')
    return value.replace('青岚县北山林区','秦岭北侧山地').replace('青岚县北山','秦岭北侧山地').replace('北山','秦岭')

def template(template_id='qinglan'):
    if template_id not in ('qinglan',QINLING):raise ValueError('未知的模拟场景模板。')
    from .initial_map import bundle
    value=deepcopy(bundle())
    if template_id=='qinglan':return value
    def texts(v):
        if isinstance(v,str):return label(v,template_id)
        if isinstance(v,list):return [texts(x) for x in v]
        if isinstance(v,dict):return {k:texts(x) for k,x in v.items()}
        return v
    value=texts(value);value['anchor']=list(ANCHOR);value['version']='20261019-qinling-north-v1'
    for row in value['records']:
        row['points']=[list(relocate(*p,template_id)) for p in row['points']]
        row['basis']+='；'+NOTICE
    return value
