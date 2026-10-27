"""Windows 8+ IFileOperation, with explicit recycle-on-delete semantics."""
import ctypes
import os
import uuid
from pathlib import Path


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def recycle(path: Path):
    if os.name != "nt":
        raise RuntimeError("휴지통 이동은 Windows에서만 지원")
    absolute = str(path.absolute())
    # No UNC/network or removable targets with unreliable recycle-bin semantics.
    kernel = ctypes.windll.kernel32
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    if absolute.startswith("\\\\") or kernel.GetDriveTypeW(Path(absolute).anchor) != 3:
        raise RuntimeError("로컬 고정 드라이브의 휴지통만 지원")
    ole = ctypes.windll.ole32
    shell = ctypes.windll.shell32
    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    ole.CoInitializeEx.argtypes = [pointer, ctypes.c_uint32]
    ole.CoInitializeEx.restype = hresult
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), pointer, ctypes.c_uint32,
                                   ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype = hresult
    shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, pointer, ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    shell.SHCreateItemFromParsingName.restype = hresult

    def check(status):
        if status < 0:
            raise OSError(f"Windows 휴지통 처리 실패: HRESULT 0x{status & 0xffffffff:08X}")

    def method(obj, index, argtypes, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        function = ctypes.WINFUNCTYPE(hresult, pointer, *argtypes)(table[index])
        result = function(obj, *args)
        check(result)
        return result

    check(ole.CoInitializeEx(None, 2))
    operation, item = pointer(), pointer()
    try:
        cls = GUID.parse("3ad05575-8857-4850-9277-11b85bdb8e09")
        interface = GUID.parse("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8")
        check(ole.CoCreateInstance(ctypes.byref(cls), None, 1, ctypes.byref(interface), ctypes.byref(operation)))
        # RECYCLEONDELETE | ADDUNDORECORD | EARLYFAILURE | NOERRORUI |
        # SILENT | NOCONFIRMATION | WANTNUKEWARNING (never silently accept a permanent-delete warning).
        # https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags
        flags = 0x80000 | 0x20000000 | 0x100000 | 0x400 | 0x4 | 0x10 | 0x4000
        method(operation, 5, [ctypes.c_uint32], flags)
        shell_interface = GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
        check(shell.SHCreateItemFromParsingName(absolute, None, ctypes.byref(shell_interface), ctypes.byref(item)))
        method(operation, 18, [pointer, pointer], item, None)
        method(operation, 21, [])
        aborted = ctypes.c_int()
        method(operation, 22, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(aborted))
        if aborted.value or path.exists():
            raise RuntimeError(f"휴지통 이동이 취소되거나 완료되지 않음: {path.name}")
    finally:
        for obj in (item, operation):
            if obj:
                table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
                ctypes.WINFUNCTYPE(ctypes.c_ulong, pointer)(table[2])(obj)
        ole.CoUninitialize()
