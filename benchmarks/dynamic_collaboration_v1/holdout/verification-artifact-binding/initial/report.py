def result(request, passed):
    return {**request, 'passed': passed}
