"""Complete synthetic structured fire report; no real geography or field measurements."""


def fire_source_label(raw_source: str) -> str:
    """Business source label; retain raw adapter IDs and legacy evidence separately."""
    label = '多模态火情预警系统'
    if any(marker in raw_source.upper() for marker in ('DEMO', '模拟', '演示')):
        label += '（演示数据）'
    return label


def confirmed_demo_message(*, source_record_id: str, timestamp: str,
                           report_no: str = '') -> dict:
    return {
        'messageId': 'message-' + source_record_id,
        'messageType': 'FIRE_EVENT', 'schemaVersion': '1.0',
        'sourceSystem': 'DEMO_CONSOLE', 'sourceRecordId': source_record_id,
        'sourceVersion': 1, 'operation': 'CREATE', 'eventTime': timestamp,
        'data': {
            'name': '结构化报警演示·青岚县北山林火',
            'reportNo': report_no or source_record_id,
            'discoveredAt': timestamp, 'status': 'confirmed',
            'location': '青岚县北山林区第三巡护段（虚构演示位置）',
            'level': 'Ⅱ级（演示）', 'adminRegion': '青岚县北山镇（虚构）',
            'initialReport': '【演示数据】模拟多模态预警系统综合可见光、热成像及巡护报告，'
                             '确认林缘出现火点后推送；现场初报有烟雾和明火，暂未报告人员被困。'
                             '以上为虚构演示内容，无真实传感器接入或现场测量。',
            'keyTargets': ['演示输电走廊', '演示林区值守站'],
            'lat': 30.1234, 'lon': 103.5678,
            'remark': '完整结构化模拟报警；地点、等级、初报、目标和坐标均为演示数据',
        },
    }
