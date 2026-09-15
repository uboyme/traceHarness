from pathlib import Path
def resolve(root, relative):
    result = Path(root) / relative
    if not str(result).startswith(str(root)): raise ValueError('outside')
    return result
