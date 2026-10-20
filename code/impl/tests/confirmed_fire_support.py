"""Named test fixtures now enter through the approved upstream CREATE contract."""
def receive_confirmed_fixture(state, fields):
    number=fields['report_no']
    data=dict(name=fields['name'],location=fields['location'],status='confirmed',
              reportNo=number,discoveredAt=fields['discovered_at'],remark='匿名测试数据')
    for field in ('lat','lon'):
        if field in fields:data[field]=fields[field]
    return state.handle_confirmed_fire_event(dict(
        messageId='fixture-'+number, messageType='FIRE_EVENT', sourceSystem='CONFIRMED_TEST_FIXTURE',
        sourceRecordId=number, sourceVersion=1, operation='CREATE', eventTime=fields['discovered_at'],
        data=data))
