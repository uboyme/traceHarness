"""Windows Unicode clipboard write with a short-lived hidden owner window."""


def copy_windows_text(text: str) -> None:
    import ctypes
    from ctypes import wintypes as w

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
    user.CreateWindowExW.restype = w.HWND
    user.DestroyWindow.argtypes = [w.HWND]
    user.OpenClipboard.argtypes = [w.HWND]
    user.EmptyClipboard.argtypes = []
    user.CloseClipboard.argtypes = []
    user.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    user.SetClipboardData.restype = w.HANDLE
    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HMODULE
    kernel.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = w.HGLOBAL
    kernel.GlobalLock.argtypes = [w.HGLOBAL]
    kernel.GlobalLock.restype = w.LPVOID
    kernel.GlobalUnlock.argtypes = [w.HGLOBAL]
    kernel.GlobalFree.argtypes = [w.HGLOBAL]
    kernel.GlobalFree.restype = w.HGLOBAL

    data = text.encode("utf-16-le") + b"\x00\x00"
    window = user.CreateWindowExW(
        0,
        "STATIC",
        "",
        0,
        0,
        0,
        0,
        0,
        None,
        None,
        kernel.GetModuleHandleW(None),
        None,
    )
    if not window:
        raise ctypes.WinError()
    opened = False
    memory = None
    try:
        # Prepare the bytes before touching the user's clipboard.
        memory = kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not memory:
            raise ctypes.WinError()
        address = kernel.GlobalLock(memory)
        if not address:
            raise ctypes.WinError()
        try:
            ctypes.memmove(address, data, len(data))
        finally:
            kernel.GlobalUnlock(memory)
        opened = bool(user.OpenClipboard(window))
        if not opened or not user.EmptyClipboard():
            raise ctypes.WinError()
        if not user.SetClipboardData(13, memory):  # CF_UNICODETEXT
            raise ctypes.WinError()
        memory = None  # Windows now owns the allocation.
    finally:
        if opened:
            user.CloseClipboard()
        if memory:
            kernel.GlobalFree(memory)
        user.DestroyWindow(window)
