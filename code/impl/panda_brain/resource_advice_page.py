"""Compact event supply tables; quantities and candidates remain source-derived."""
from datetime import datetime,timedelta,timezone
from .workbench import esc
from .resource_advisor import assess


def body(state,event_id):
    from .resource_pages import table
    data=assess(state,event_id)
    def q(value):return '待核' if value is None else format(value,'.3g')
    stamp=datetime.fromisoformat(data['generated_at']).astimezone(timezone(timedelta(hours=8))).strftime('%H:%M:%S')
    out=['<div id="resource-advice" data-event="'+esc(event_id)+'"><section class="lz-card resource-section"><div class="resource-heading"><h2>物资预警与支援推荐</h2><small id="resource-update-state" role="status">'+esc(stamp)+'</small></div>']
    needs=[];points=[];companies=[]
    for row in data['rows']:
        gap=row['current_gap'];forecast=row['forecast_gap'];hours=row['coverage_hours']
        timing='已不足' if gap is not None and gap>0 else '约 '+q(hours)+' 小时后' if forecast is not None and forecast>0 and hours is not None else '待核' if hours is None else '—'
        status=row['status']+('；运输需加急' if row['late_supply_risk'] else '')
        name=row['name']+'（'+row['unit']+'）'
        needs.append((name,q(row['available']),q(row['current_need']),q(gap),q(row['forecast_need']),q(forecast),timing,status))
        for candidate in row['points'][:2]:
            points.append((row['name'],candidate['name'],candidate['location'],q(candidate['quantity'])+' '+row['unit'],q(candidate['distance_km']),q(candidate['lead_hours']),candidate['contact'],candidate['phone']))
        for candidate in row['companies'][:2]:
            companies.append((row['name'],candidate['name'],candidate['region'],'待联系' if candidate['quantity'] is None else q(candidate['quantity'])+' '+row['unit'],q(candidate['distance_km']),q(candidate['lead_hours']),candidate['contact'],candidate['phone']))
    out.append(table(('物资','现场可用','当前需求','当前缺口','预计需求','预计缺口','预计不足时间','状态'),needs))
    if data['issues']:out.append(table(('资料状态','内容'),[('待核',issue) for issue in data['issues']]))
    out.append('<h3>就近物资库</h3>'+table(('物资','物资库','位置','可供数量','直线距离(km)','预计送达(h)','联系人','电话'),points))
    out.append('<h3>外协公司推荐</h3>'+table(('物资','公司','服务区域','可供数量','直线距离(km)','预计送达(h)','联系人','电话'),companies))
    return ''.join(out)+'</section></div>'
