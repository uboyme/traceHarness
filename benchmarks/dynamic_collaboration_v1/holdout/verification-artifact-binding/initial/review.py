def accept(request, report):
    return bool(report['passed'])
