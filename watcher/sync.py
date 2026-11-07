"""Keep DATA_DIR in a private Hugging Face dataset repo so a Space restart loses nothing.

On startup: download the repo and merge it into DATA_DIR (this device's own local files win,
other devices' files are refreshed). Every 30 minutes, after alerts, and on shutdown: one
commit with only this device's changed files. Without HF_TOKEN/HF_DATASET it does nothing.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("watcher.sync")


class DatasetSync:
    def __init__(self, token: str, repo_id: str, data_dir: Path, running_on: str):
        self.token = token
        self.repo_id = repo_id
        self.data_dir = Path(data_dir)
        self.running_on = running_on
        self.enabled = bool(token and repo_id)
        self.last_error: str | None = None
        self._api = None

    def _hf(self):
        if self._api is None:
            from huggingface_hub import HfApi  # imported lazily

            self._api = HfApi(token=self.token)
        return self._api

    def _own(self, rel: str) -> bool:
        name = Path(rel).name
        if rel.startswith("history/"):
            return name.startswith(f"{self.running_on}-")
        if rel.startswith("state/"):
            return name == f"{self.running_on}.json"
        return True  # shared small files (overrides, watchlist cache)

    def restore(self) -> str:
        if not self.enabled:
            return "local only"
        try:
            from huggingface_hub import snapshot_download

            self._hf().create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix="pw-restore-"))
            try:
                snapshot_download(repo_id=self.repo_id, repo_type="dataset", local_dir=tmp, token=self.token,
                                  allow_patterns=["history/*.csv", "state/*.json", "*.json"])
                copied = self._merge(tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self.last_error = None
            return f"restored {copied} file(s) from {self.repo_id}"
        except ImportError:
            self.enabled = False
            return "huggingface_hub not installed: history stays local"
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.warning("dataset restore failed: %s", exc)
            return f"restore failed ({self.last_error})"

    def _merge(self, source: Path) -> int:
        copied = 0
        for path in source.rglob("*"):
            if not path.is_file() or ".cache" in path.parts or path.name.startswith("."):
                continue
            rel = path.relative_to(source).as_posix()
            target = self.data_dir / rel
            if target.exists() and self._own(rel):
                # Our own file: the local copy is newer unless it is shorter (e.g. a fresh folder).
                if target.stat().st_size >= path.stat().st_size:
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
        return copied

    def push(self, paths, message: str) -> bool:
        if not self.enabled:
            return False
        files = sorted({Path(p) for p in paths if Path(p).is_file()})
        if not files:
            return True
        try:
            from huggingface_hub import CommitOperationAdd

            operations = [
                CommitOperationAdd(path_in_repo=p.relative_to(self.data_dir).as_posix(), path_or_fileobj=str(p))
                for p in files
            ]
            self._hf().create_commit(self.repo_id, operations, commit_message=message, repo_type="dataset")
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.warning("dataset push failed: %s", exc)
            return False
