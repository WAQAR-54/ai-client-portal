"""Removing uploaded files together with the rows that own them.

Django deletes the database row but never the file behind a FileField/ImageField. So a
conversation removed by the retention sweep (or a user, or an edit that drops later turns) left
its attachments on disk for good - a privacy problem for a department that set a retention
period, and unbounded disk growth otherwise.
"""

from django.db import transaction


def delete_file_after_commit(field_file):
    """Delete `field_file`'s stored file once the surrounding transaction commits.

    After the commit, not immediately: if the delete of the row is rolled back, the row still
    needs its file. A missing file is not an error (it may already have been cleaned up)."""
    name = field_file.name if field_file else ""
    if not name:
        return
    storage = field_file.storage

    def remove():
        try:
            storage.delete(name)
        except OSError:
            pass  # best effort: a leftover file is a disk-space issue, never a failed request

    transaction.on_commit(remove)
