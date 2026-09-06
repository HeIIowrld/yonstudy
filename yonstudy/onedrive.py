"""OneDrive로 나가던 이전 API의 호환 모듈.

새 코드에서는 :mod:`yonstudy.remote`를 사용한다.
"""

from .remote import DirectSyncResult, RemoteStorageError, RcloneRemote, sync_remote_tree

OneDriveError = RemoteStorageError
RcloneOneDrive = RcloneRemote
sync_onedrive_tree = sync_remote_tree

__all__ = [
    "DirectSyncResult",
    "OneDriveError",
    "RcloneOneDrive",
    "sync_onedrive_tree",
]
