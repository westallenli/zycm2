"""Opt-in appearance for newly hand-drawn objects; old annotations keep their renderer."""
VERSION = 'reference-v1'
SHAPES = {'point': 'text', 'circle': 'area', 'ellipse': 'area',
          'rectangle': 'area', 'polygon': 'area', 'line': 'route',
          'curve': 'route', 'arrow': 'arrow', 'text': 'text'}
COLORS = {'yellow': '#FFD83D', 'purple': '#8661FF', 'cyan': '#00BBF2', 'red': '#FF5364'}
FIELDS = frozenset({'manual_style', 'manual_shape', 'manual_color'})
LABELS = {'point':'点', 'circle':'圆', 'ellipse':'椭圆', 'rectangle':'矩形',
          'polygon':'多边形', 'line':'折线', 'curve':'曲线', 'arrow':'宽尾弯箭头', 'text':'文字'}


def validate(kind, details):
    if not any(details.get(key) for key in FIELDS):
        return
    if details.get('manual_style') != VERSION:
        raise ValueError('手动标绘样式版本无效')
    shape = details.get('manual_shape', '')
    if shape and SHAPES.get(shape) != kind:
        raise ValueError('图形与标绘类型不一致，请重新选择工具')
    if details.get('manual_color', '') not in ('', *COLORS):
        raise ValueError('请选择工具提供的颜色')


def presentation(mark):
    if mark.detail('manual_style') != VERSION:
        return {}
    kind = getattr(mark.kind, 'value', str(mark.kind))
    shape = mark.detail('manual_shape')
    if shape and SHAPES.get(shape) != kind:
        return {}
    color = COLORS.get(mark.detail('manual_color'), '#FF653B')
    return {'version': VERSION, 'shape': shape, 'label': LABELS.get(shape,''), 'fill': color,
            'stroke': '#E65A27' if shape == 'arrow' else color}
