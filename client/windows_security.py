"""Small fail-closed Win32 primitives shared by the client and managed installer.

The client and managed updater cannot rely on POSIX mode bits on Windows.  This
module therefore checks whether an untrusted local principal can access a
protected path, holds a non-inheritable directory handle without delete
sharing, and performs same-volume metadata moves with write-through semantics.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class WindowsSecurityError(RuntimeError):
    """A Windows security or durability primitive could not be established."""


_TRUSTED_WELL_KNOWN_SIDS = {
    "S-1-3-0",       # Creator Owner (inherited placeholder)
    "S-1-3-4",       # Owner Rights
    "S-1-5-18",      # Local System
    "S-1-5-32-544",  # Builtin Administrators
}
_TRUSTED_OWNER_SIDS = {
    "S-1-5-18",      # Local System
    "S-1-5-32-544",  # Builtin Administrators
}
_MUTATION_ACCESS_MASK = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x02000000  # MAXIMUM_ALLOWED (fail closed if encoded in an allow ACE)
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
_DATA_ACCESS_MASK = (
    _MUTATION_ACCESS_MASK
    | 0x00000001  # FILE_READ_DATA / FILE_LIST_DIRECTORY
    | 0x00000008  # FILE_READ_EA
    | 0x00000020  # FILE_EXECUTE / FILE_TRAVERSE
    | 0x00000080  # FILE_READ_ATTRIBUTES
    | 0x00020000  # READ_CONTROL
    | 0x20000000  # GENERIC_EXECUTE
    | 0x80000000  # GENERIC_READ
)


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsSecurityError("Win32 primitive requested on a non-Windows host")


def _win_libraries():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_uint
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetSecurityInfo.restype = wintypes.DWORD
    return kernel32, advapi32


def _sid_string(pointer, *, advapi32, kernel32) -> str:
    import ctypes
    from ctypes import wintypes

    if not pointer or not advapi32.IsValidSid(pointer):
        raise WindowsSecurityError("Windows security descriptor contains an invalid SID")
    rendered = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(pointer, ctypes.byref(rendered)):
        raise WindowsSecurityError("could not render a Windows security principal")
    try:
        value = rendered.value
        if not value:
            raise WindowsSecurityError("Windows security principal is empty")
        return value.upper()
    finally:
        kernel32.LocalFree(rendered)


def _current_user_sid(*, advapi32, kernel32) -> str:
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise WindowsSecurityError("could not open the current Windows process token")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise WindowsSecurityError("could not size the current Windows user token")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, required.value, ctypes.byref(required)
        ):
            raise WindowsSecurityError("could not read the current Windows user token")
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        return _sid_string(user.User.Sid, advapi32=advapi32, kernel32=kernel32)
    finally:
        kernel32.CloseHandle(token)


def _open_security_handle(path: Path, desired_access: int):
    """Open and pin one real filesystem object for ACL inspection or mutation."""

    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32, advapi32 = _win_libraries()
    handle = kernel32.CreateFileW(
        str(Path(path)),
        desired_access,
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; no delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise WindowsSecurityError("could not pin the Windows path for ACL access")
    information = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        kernel32.CloseHandle(handle)
        raise WindowsSecurityError("could not inspect the pinned Windows path")
    if information.dwFileAttributes & 0x00000400:
        kernel32.CloseHandle(handle)
        raise WindowsSecurityError("Windows ACL target is a reparse point")
    return (
        handle,
        bool(information.dwFileAttributes & 0x00000010),
        kernel32,
        advapi32,
    )


def _validate_trusted_owner_handle(handle, *, advapi32, kernel32) -> str:
    """Require a trusted owner before any ACL mutation is attempted."""

    import ctypes

    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetSecurityInfo(
        handle,
        1,  # SE_FILE_OBJECT
        0x00000001,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        raise WindowsSecurityError(
            f"could not read the Windows path owner (error {status})"
        )
    try:
        current_user = _current_user_sid(advapi32=advapi32, kernel32=kernel32)
        owner_sid = _sid_string(owner, advapi32=advapi32, kernel32=kernel32)
        if owner_sid not in _TRUSTED_OWNER_SIDS | {current_user}:
            raise WindowsSecurityError(
                "Windows path owner is not trusted for managed mutation"
            )
        return current_user
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _validate_private_acl_handle(
    handle,
    *,
    target_is_directory: bool,
    forbidden_access_mask: int,
    access_label: str,
    advapi32,
    kernel32,
) -> None:
    """Reject unsafe ACEs on the already-pinned filesystem object."""

    import ctypes
    from ctypes import wintypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetSecurityInfo(
        handle,
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        raise WindowsSecurityError(f"could not read the Windows path ACL (error {status})")
    try:
        current_user = _current_user_sid(advapi32=advapi32, kernel32=kernel32)
        owner_sid = _sid_string(owner, advapi32=advapi32, kernel32=kernel32)
        if owner_sid not in _TRUSTED_OWNER_SIDS | {current_user}:
            raise WindowsSecurityError(
                "Windows path owner is not trusted for managed mutation"
            )
        trusted = set(_TRUSTED_WELL_KNOWN_SIDS)
        trusted.add(current_user)
        if not dacl.value:
            raise WindowsSecurityError("Windows path has an unrestricted null DACL")

        details = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(details), ctypes.sizeof(details), 2
        ):
            raise WindowsSecurityError("could not inspect the Windows path ACL")
        for index in range(details.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise WindowsSecurityError("could not inspect a Windows path access rule")
            header = ctypes.cast(ace_pointer, ctypes.POINTER(AceHeader)).contents
            if header.AceSize < 8:
                raise WindowsSecurityError("Windows path ACL contains a truncated access rule")
            inherit_only = bool(header.AceFlags & 0x08)  # INHERIT_ONLY_ACE
            mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
            if not mask & forbidden_access_mask:
                continue
            # Deny/audit/alarm ACEs do not grant mutation.  Object/callback
            # allow ACEs have variable SID offsets, so reject those writable
            # layouts until they are parsed explicitly rather than guessing.
            if header.AceType in {1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15}:
                continue
            if inherit_only and not target_is_directory:
                continue
            if header.AceType != 0 or header.AceSize < 12:
                raise WindowsSecurityError(
                    "Windows path has an unsupported writable access rule"
                )
            sid = ctypes.c_void_p(ace_pointer.value + 8)
            principal = _sid_string(sid, advapi32=advapi32, kernel32=kernel32)
            if principal not in trusted:
                raise WindowsSecurityError(
                    f"Windows path grants {access_label} rights to an untrusted principal"
                )
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _validate_private_acl(
    path: Path, *, forbidden_access_mask: int, access_label: str
) -> None:
    """Reject an ACL that grants selected access to an untrusted principal."""

    if os.name != "nt":
        return
    _require_windows()
    handle, target_is_directory, kernel32, advapi32 = _open_security_handle(
        path, 0x00020000  # READ_CONTROL
    )
    try:
        _validate_private_acl_handle(
            handle,
            target_is_directory=target_is_directory,
            forbidden_access_mask=forbidden_access_mask,
            access_label=access_label,
            advapi32=advapi32,
            kernel32=kernel32,
        )
    finally:
        kernel32.CloseHandle(handle)


def validate_private_mutation_acl(path: Path) -> None:
    """Reject a Windows path writable by a principal outside the local trust set.

    Read-only inherited entries are allowed: the managed source tree is not a
    secret.  Any allow ACE carrying file/directory mutation rights must belong
    to the current user, Local System, Builtin Administrators, or an owner
    placeholder.  Unsupported allow-ACE layouts fail closed when writable.
    """

    _validate_private_acl(
        path,
        forbidden_access_mask=_MUTATION_ACCESS_MASK,
        access_label="mutation",
    )


def validate_private_data_acl(path: Path) -> None:
    """Reject a Windows data path readable or writable by an untrusted principal."""

    _validate_private_acl(
        path,
        forbidden_access_mask=_DATA_ACCESS_MASK,
        access_label="data-access",
    )


def validate_private_data_fd(fd: int) -> None:
    """Validate the DACL on an already-open Windows file descriptor.

    The caller retains ownership of ``fd``.  Inspecting the same underlying
    handle that will be read avoids a path swap between ACL validation and use.
    """

    if os.name != "nt":
        return
    _require_windows()
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError as exc:
        raise WindowsSecurityError(
            "could not access the open Windows file for ACL validation"
        ) from exc
    if handle == -1:
        raise WindowsSecurityError(
            "could not access the open Windows file for ACL validation"
        )
    kernel32, advapi32 = _win_libraries()
    _validate_private_acl_handle(
        handle,
        target_is_directory=False,
        forbidden_access_mask=_DATA_ACCESS_MASK,
        access_label="data-access",
        advapi32=advapi32,
        kernel32=kernel32,
    )


def _harden_private_data_acl(path: Path, *, require_directory: bool) -> None:
    import ctypes
    from ctypes import wintypes

    handle, target_is_directory, kernel32, advapi32 = _open_security_handle(
        path,
        0x00020000 | 0x00040000,  # READ_CONTROL | WRITE_DAC
    )
    try:
        if target_is_directory != require_directory:
            expected = "directory" if require_directory else "file"
            raise WindowsSecurityError(
                f"private Windows path is not a real {expected}"
            )
        current_user = _validate_trusted_owner_handle(
            handle, advapi32=advapi32, kernel32=kernel32
        )
        descriptor = ctypes.c_void_p()
        descriptor_size = wintypes.DWORD()
        inheritance = "OICI" if require_directory else ""
        sddl = (
            "D:P"
            f"(A;{inheritance};FA;;;{current_user})"
            f"(A;{inheritance};FA;;;SY)"
            f"(A;{inheritance};FA;;;BA)"
        )
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            if descriptor:
                kernel32.LocalFree(descriptor)
            raise WindowsSecurityError("could not build the private Windows directory ACL")
        try:
            dacl_present = wintypes.BOOL()
            dacl_defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(dacl_present),
                ctypes.byref(dacl),
                ctypes.byref(dacl_defaulted),
            ):
                raise WindowsSecurityError("could not read the private Windows directory ACL")
            if not dacl_present.value or not dacl.value:
                raise WindowsSecurityError("private Windows directory ACL is unrestricted")
            status = advapi32.SetSecurityInfo(
                handle,
                1,  # SE_FILE_OBJECT
                0x00000004 | 0x80000000,  # DACL + PROTECTED_DACL_SECURITY_INFORMATION
                None,
                None,
                dacl,
                None,
            )
            if status != 0:
                raise WindowsSecurityError(
                    f"could not protect the private Windows directory ACL (error {status})"
                )
        finally:
            if descriptor:
                kernel32.LocalFree(descriptor)
        _validate_private_acl_handle(
            handle,
            target_is_directory=True,
            forbidden_access_mask=_DATA_ACCESS_MASK,
            access_label="data-access",
            advapi32=advapi32,
            kernel32=kernel32,
        )
    finally:
        kernel32.CloseHandle(handle)


def harden_private_data_acl(path: Path) -> None:
    """Protect a Windows directory DACL for the user, System, and Administrators.

    The protected inheritable DACL prevents a broader application/profile parent
    from making sensitive descendant files readable to another local principal.
    """

    if os.name != "nt":
        return
    _require_windows()
    _harden_private_data_acl(path, require_directory=True)


def harden_private_data_file_acl(path: Path) -> None:
    """Protect an existing Windows data file without rewriting its contents."""

    if os.name != "nt":
        return
    _require_windows()
    _harden_private_data_acl(path, require_directory=False)


class PinnedWindowsDirectory:
    """Hold a directory identity for repeated boundary validation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None
        self._identity = None
        self._kernel32 = None

    @staticmethod
    def _information_type():
        import ctypes
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        return ByHandleFileInformation

    def _information(self):
        import ctypes

        information_type = self._information_type()
        information = information_type()
        if not self._kernel32.GetFileInformationByHandle(
            self._handle, ctypes.byref(information)
        ):
            raise WindowsSecurityError("could not inspect the pinned Windows directory")
        if not information.dwFileAttributes & 0x00000010:
            raise WindowsSecurityError("pinned Windows root is not a directory")
        if information.dwFileAttributes & 0x00000400:
            raise WindowsSecurityError("pinned Windows root is a reparse point")
        return (
            information.dwVolumeSerialNumber,
            information.nFileIndexHigh,
            information.nFileIndexLow,
            information.dwFileAttributes & (0x00000010 | 0x00000400),
        )

    def _path_identity(self):
        try:
            information = os.lstat(self.path)
        except OSError as exc:
            raise WindowsSecurityError("managed Windows root path is unavailable") from exc
        attributes = getattr(information, "st_file_attributes", 0)
        if not attributes & 0x00000010 or attributes & 0x00000400:
            raise WindowsSecurityError("managed Windows root path changed type")
        return (
            information.st_dev & 0xFFFFFFFF,
            (information.st_ino >> 32) & 0xFFFFFFFF,
            information.st_ino & 0xFFFFFFFF,
            attributes & (0x00000010 | 0x00000400),
        )

    def __enter__(self):
        import ctypes

        _require_windows()
        kernel32, _advapi32 = _win_libraries()
        self._kernel32 = kernel32
        handle = kernel32.CreateFileW(
            str(self.path),
            0x00000080,  # FILE_READ_ATTRIBUTES
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; no delete
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise WindowsSecurityError("could not pin the managed Windows root")
        self._handle = handle
        if not kernel32.SetHandleInformation(handle, 0x00000001, 0):
            kernel32.CloseHandle(handle)
            self._handle = None
            raise WindowsSecurityError("could not make the root handle non-inheritable")
        try:
            self._identity = self._information()
        except WindowsSecurityError:
            kernel32.CloseHandle(handle)
            self._handle = None
            raise
        return self

    def validate(self) -> None:
        if (
            self._handle is None
            or self._information() != self._identity
            or self._path_identity() != self._identity
        ):
            raise WindowsSecurityError("pinned Windows root identity changed during update")

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def move_write_through(source: Path, destination: Path, *, replace_existing: bool) -> None:
    """Move within the managed volume and wait for the move to reach disk."""

    if os.name != "nt":
        if replace_existing:
            os.replace(source, destination)
            return
        import ctypes
        import errno

        libc = ctypes.CDLL(None, use_errno=True)
        source_bytes = os.fsencode(source)
        destination_bytes = os.fsencode(destination)
        if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
            renameat2 = libc.renameat2
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100, source_bytes, -100, destination_bytes, 0x00000001
            )  # AT_FDCWD, RENAME_NOREPLACE
        elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
            renamex = libc.renamex_np
            renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex.restype = ctypes.c_int
            result = renamex(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
        else:
            raise WindowsSecurityError("atomic no-replace move is unavailable on this platform")
        if result != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError(code, os.strerror(code), str(destination))
            raise OSError(code, os.strerror(code), str(source))
        return
    _require_windows()
    import ctypes

    kernel32, _advapi32 = _win_libraries()
    flags = 0x00000008  # MOVEFILE_WRITE_THROUGH
    if replace_existing:
        flags |= 0x00000001  # MOVEFILE_REPLACE_EXISTING
    if not kernel32.MoveFileExW(str(source), str(destination), flags):
        code = ctypes.get_last_error()
        if not replace_existing and code in {80, 183}:  # ERROR_FILE_EXISTS / ALREADY_EXISTS
            raise FileExistsError(code, "destination already exists", str(destination))
        raise WindowsSecurityError(f"durable Windows move failed (error {code})")
