def parse(value, max_value):
    value = int(value)
    if value < 1 or value > max_value: raise ValueError('range')
    return value
