def total(attempts):
    return sum(row['tokens'] or 0 for row in attempts if row['status'] == 'completed')
