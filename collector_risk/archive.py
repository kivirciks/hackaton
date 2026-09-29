"""Потоковое чтение CSV из 7z через системную libarchive."""
import ctypes as ct
import io

_lib = ct.CDLL('libarchive.so.13')
_lib.archive_read_new.restype = ct.c_void_p
for name in ('archive_read_support_filter_all', 'archive_read_support_format_all', 'archive_read_free'):
    getattr(_lib, name).argtypes = [ct.c_void_p]
_lib.archive_read_open_filename.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_size_t]
_lib.archive_read_open_filename.restype = ct.c_int
_lib.archive_read_next_header.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
_lib.archive_read_next_header.restype = ct.c_int
_lib.archive_read_data.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_size_t]
_lib.archive_read_data.restype = ct.c_ssize_t


class ArchiveCSV(io.RawIOBase):
    """Предоставляет CSV внутри 7z как поток для чтения pandas без полной распаковки."""
    def __init__(self, path):
        """Открывает архив и первый CSV-элемент через системную библиотеку libarchive."""
        self.handle = _lib.archive_read_new()
        _lib.archive_read_support_filter_all(self.handle)
        _lib.archive_read_support_format_all(self.handle)
        entry = ct.c_void_p()
        if (_lib.archive_read_open_filename(self.handle, str(path).encode(), 10240) != 0
                or _lib.archive_read_next_header(self.handle, ct.byref(entry)) != 0):
            self.close()
            raise OSError(f'Cannot open 7z CSV: {path}')

    def readable(self):
        """Сообщает потребителю, что поток поддерживает чтение."""
        return True

    def readinto(self, buffer):
        """Распаковывает следующую порцию байтов в переданный буфер."""
        target = (ct.c_char * len(buffer)).from_buffer(buffer)
        count = _lib.archive_read_data(self.handle, target, len(buffer))
        if count < 0:
            raise OSError('7z decompression error')
        return count

    def close(self):
        """Освобождает дескриптор архива и закрывает поток."""
        if getattr(self, 'handle', None):
            _lib.archive_read_free(self.handle)
            self.handle = None
        super().close()
