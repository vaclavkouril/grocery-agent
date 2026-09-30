import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from grocery_agent.persistence.base import StoredSnapshot
from grocery_agent.stores.base import SourceEvidence


class FileSnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, evidence: SourceEvidence) -> StoredSnapshot:
        digest = hashlib.sha256(evidence.content).hexdigest()
        relative = Path(digest[:2]) / f"{digest}.bin"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                try:
                    temporary.write(evidence.content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                except BaseException:
                    temporary_path.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredSnapshot(str(uuid4()), digest, relative.as_posix(), evidence)
