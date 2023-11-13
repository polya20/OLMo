import argparse
import logging
import os
import re
import shutil
import tarfile
import tempfile
from abc import ABC, abstractmethod
from argparse import ArgumentParser, _SubParsersAction
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import boto3
import botocore.exceptions as boto_exceptions
import google.cloud.storage as gcs
from botocore.config import Config
from google.api_core.exceptions import NotFound
from rich.progress import Progress

from olmo import util
from olmo.aliases import PathOrStr

log = logging.getLogger(__name__)


R2_ACCOUNT_ID: str = "a198dc34621661a1a66a02d6eb7c4dc3"
DEFAULT_MAX_ARCHIVE_SIZE: float = 5_000_000_000  # 5GB


class CleaningOperations(Enum):
    DELETE_BAD_RUNS = auto()


class StorageType(Enum):
    LOCAL_FS = auto()
    GCS = auto()
    S3 = auto()
    R2 = auto()


class StorageAdapter(ABC):
    @abstractmethod
    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        """Lists all the entries within the directory or compressed file at the given path.
        Returns only top-level entries (i.e. not entries in subdirectories).

        max_file_size sets a threshold for the largest size file to retain within entries.
        Any file of larger size is not included in the returned results.
        """

    @abstractmethod
    def list_dirs(self, path: str) -> List[str]:
        """Lists all the directories within the directory or compressed file at the given path.
        Returns only top-level entries (i.e. not entries in subdirectories).
        """

    @abstractmethod
    def delete_path(self, path: str):
        """Deletes the entry at the given path and, if the path is a directory, delete all entries
        within its subdirectories.
        """

    @abstractmethod
    def is_file(self, path: str) -> bool:
        """Returns whether the given path corresponds to an existing file."""

    @classmethod
    def create_storage_adapter(cls, storage_type: StorageType, r2_account_id: Optional[str] = None):
        if storage_type == StorageType.LOCAL_FS:
            return LocalFileSystemAdapter()
        if storage_type == StorageType.GCS:
            return GoogleCloudStorageAdapter()
        if storage_type == StorageType.S3:
            return S3StorageAdapter()
        if storage_type == StorageType.R2:
            if r2_account_id is None:
                raise ValueError("R2 account id must be provided to create R2 storage adapter")
            return S3StorageAdapter(endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com")

        raise NotImplementedError(f"No storage adapter implemented for storage type {storage_type}")

    @staticmethod
    def _is_url(path: str) -> bool:
        return re.match(r"[a-z0-9]+://.*", path) is not None

    @staticmethod
    def get_storage_type_for_path(path: str) -> StorageType:
        if StorageAdapter._is_url(path):
            parsed = urlparse(str(path))
            if parsed.scheme == "gs":
                return StorageType.GCS
            elif parsed.scheme == "s3":
                return StorageType.S3
            elif parsed.scheme == "r2":
                return StorageType.R2
            elif parsed.scheme == "file":
                return StorageType.LOCAL_FS

        return StorageType.LOCAL_FS


class LocalFileSystemAdapter(StorageAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._temp_files: List[tempfile._TemporaryFileWrapper[bytes]] = []
        self._archive_extensions: List[str] = [
            extension.lower() for _, extensions, _ in shutil.get_unpack_formats() for extension in extensions
        ]

    def __del__(self):
        for temp_file in self._temp_files:
            temp_file.close()

    def create_temp_file(self, suffix: Optional[str] = None) -> str:
        temp_file = tempfile.NamedTemporaryFile(suffix=suffix)
        self._temp_files.append(temp_file)
        return temp_file.name

    def has_supported_archive_extension(self, path: PathOrStr) -> bool:
        filename = Path(path).name.lower()
        return any(filename.endswith(extension) for extension in self._archive_extensions)

    def _list_entries(
        self, path: PathOrStr, include_files: bool = True, max_file_size: Optional[float] = None
    ) -> List[str]:
        path = Path(path)
        if path.is_dir():
            return [
                entry.name
                for entry in path.iterdir()
                if (
                    (include_files or not entry.is_file())
                    and (max_file_size is None or entry.stat().st_size <= max_file_size)
                )
            ]

        if self.has_supported_archive_extension(path):
            if include_files or max_file_size is not None:
                raise NotImplementedError("Filtering out entries from a tar file is not yet supported")

            with tarfile.open(path) as tar:
                log.info("Listing entries from archive %s", path)
                return [
                    Path(tar_subpath).name for tar_subpath in tar.getnames() if len(Path(tar_subpath).parts) == 2
                ]

        raise ValueError(f"Path does not correspond to directory or supported archive file: {path}")

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, include_files=False)

    def delete_path(self, path: str):
        path_obj = Path(path)
        if not path_obj.exists():
            return

        if path_obj.is_file():
            path_obj.unlink()
        else:
            shutil.rmtree(path)

    def is_file(self, path: str) -> bool:
        path_obj = Path(path)
        if not path_obj.exists():
            return False

        return path_obj.is_file()


class GoogleCloudStorageAdapter(StorageAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._local_fs_adapter: Optional[LocalFileSystemAdapter] = None
        self._gcs_client: Optional[gcs.Client] = None
        self._temp_dirs: List[tempfile.TemporaryDirectory] = []

    @property
    def local_fs_adapter(self):
        if self._local_fs_adapter is None:
            self._local_fs_adapter = LocalFileSystemAdapter()

        return self._local_fs_adapter

    @property
    def gcs_client(self):
        if self._gcs_client is None:
            self._gcs_client = gcs.Client()

        return self._gcs_client

    @staticmethod
    def _get_bucket_name_and_key(path: str) -> Tuple[str, str]:
        parsed_path = urlparse(path)
        bucket_name = parsed_path.netloc
        key = parsed_path.path.lstrip("/")
        return bucket_name, key

    def _get_blob_size(self, blob: gcs.Blob) -> int:
        blob.reload()
        if blob.size is None:
            raise ValueError(f"Failed to get size for blob: {blob.name}")
        return blob.size

    def _is_file(self, bucket_name: str, key: str) -> bool:
        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.blob(key)
        try:
            blob.reload()
            print(blob.name)
            return True
        except NotFound:
            return False

    def _get_size(self, bucket_name: str, key: str) -> int:
        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.get_blob(key)
        if blob is None:
            raise ValueError(f"Getting size for invalid object with bucket | key: {bucket_name} | {key}")

        return self._get_blob_size(blob)

    def _download_file(self, bucket_name: str, key: str) -> str:
        extension = "".join(Path(key).suffixes)
        temp_file = self.local_fs_adapter.create_temp_file(suffix=extension)

        bucket = self.gcs_client.bucket(bucket_name)
        blob = bucket.get_blob(key)
        if blob is None:
            raise ValueError(f"Downloading invalid object with bucket | key: {bucket_name} | {key}")
        blob.download_to_filename(temp_file)
        return temp_file

    def _get_directory_entries(
        self,
        bucket_name: str,
        key: str,
        include_files: bool = True,
        max_file_size: Optional[float] = None,
    ) -> List[str]:
        bucket = self.gcs_client.bucket(bucket_name)
        # Setting max_results to 10,000 as a reasonable caution that a directory should not have
        # more than 10,000 entries.
        # Using delimiter causes result to have directory-like structure
        blobs = bucket.list_blobs(max_results=10_000, prefix=key, delimiter="/")

        entries: List[str] = []
        for blob in blobs:
            if not include_files:
                # Note: We need to iterate through (or otherwise act on?) the blobs to populate blob.prefixes
                # Thus we no-op here rather than skipping the loop
                continue

            size: int = self._get_blob_size(blob)
            if max_file_size is not None and size > max_file_size:
                log.info(
                    "Blob %s has size %.2fGb exceeding max file size %.2fGb, skipping.",
                    blob.name,
                    size / 1e9,
                    max_file_size / 1e9,
                )
                continue

            entries.append(blob.name)  # type: ignore

        # Note: We need to iterate through (or otherwise act on?) the blobs to populate blob.prefixes
        entries += blobs.prefixes

        return [entry.removeprefix(key) for entry in entries]

    def _list_entries(self, path: str, include_files: bool = True, max_file_size: Optional[float] = None) -> List[str]:
        bucket_name, key = self._get_bucket_name_and_key(path)

        if self.local_fs_adapter.has_supported_archive_extension(path):
            log.info("Downloading archive %s", path)
            file_path = self._download_file(bucket_name, key)

            if not include_files:
                return self.local_fs_adapter.list_dirs(file_path)
            return self.local_fs_adapter.list_entries(file_path, max_file_size)

        if self._is_file(bucket_name, key):
            raise ValueError(f"Path corresponds to a file without a supported archive extension {path}")

        res = self._get_directory_entries(bucket_name, key, include_files=include_files, max_file_size=max_file_size)
        return res

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, include_files=False)

    def delete_path(self, path: str):
        bucket_name, key = self._get_bucket_name_and_key(path)

        bucket = self.gcs_client.bucket(bucket_name)
        # Not using delimiter causes result to not have directory-like structure (all blobs returned)
        blobs = list(bucket.list_blobs(prefix=key))

        bucket.delete_blobs(blobs)

    def is_file(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)

        return self._is_file(bucket_name, key)


class S3StorageAdapter(StorageAdapter):
    def __init__(self, endpoint_url: Optional[str] = None):
        super().__init__()
        self._s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 10, "mode": "standard"}),
            use_ssl=not int(os.environ.get("OLMO_NO_SSL", "0")),
        )

        self._local_fs_adapter: Optional[LocalFileSystemAdapter] = None
        self._temp_dirs: List[tempfile.TemporaryDirectory] = []

    @property
    def local_fs_adapter(self):
        if self._local_fs_adapter is None:
            self._local_fs_adapter = LocalFileSystemAdapter()

        return self._local_fs_adapter

    @staticmethod
    def _get_bucket_name_and_key(path: str) -> Tuple[str, str]:
        parsed_path = urlparse(path)
        bucket_name = parsed_path.netloc
        key = parsed_path.path.lstrip("/")
        return bucket_name, key

    def _download_file(self, bucket_name: str, key: str) -> str:
        extension = "".join(Path(key).suffixes)
        temp_file = self.local_fs_adapter.create_temp_file(suffix=extension)

        head_response: Dict[str, Any] = self._s3_client.head_object(Bucket=bucket_name, Key=key)
        if "ContentLength" not in head_response:
            raise RuntimeError(f"Failed to get size for file with bucket | key: {bucket_name} | {key}")
        size_in_bytes: int = head_response["ContentLength"]

        with Progress(transient=True) as progress:
            download_task = progress.add_task(f"Downloading {key}", total=size_in_bytes)

            def progress_callback(bytes_downloaded: int):
                progress.update(download_task, advance=bytes_downloaded)

            self._s3_client.download_file(bucket_name, key, temp_file, Callback=progress_callback)

        if not self.local_fs_adapter.is_file(temp_file):
            raise RuntimeError(f"Failed to download file with bucket | key: {bucket_name} | {key}")

        return temp_file

    def _get_directory_entries(
        self,
        bucket_name: str,
        key: str,
        include_files: bool = True,
        max_file_size: Optional[float] = None,
    ) -> List[str]:
        response: Dict[str, Any] = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key, Delimiter="/")

        entries: List[str] = []

        if include_files:
            objects_metadata: List[Dict[str, Any]] = response.get("Contents", [])
            for object_metadata in objects_metadata:
                object_name = object_metadata["Key"]

                size: int = object_metadata["Size"]
                if max_file_size is not None and size > max_file_size:
                    log.info(
                        "Object %s has size %.2fGb exceeding max file size %.2fGb, skipping.",
                        object_name,
                        size / 1e9,
                        max_file_size / 1e9,
                    )
                    continue

                entries.append(object_name)

        directories_metadata: List[Dict[str, str]] = response.get("CommonPrefixes", [])
        entries += [directory_metadata["Prefix"] for directory_metadata in directories_metadata]

        return [entry.removeprefix(key) for entry in entries]

    def _list_entries(self, path: str, include_files: bool = True, max_file_size: Optional[float] = None) -> List[str]:
        bucket_name, key = self._get_bucket_name_and_key(path)

        if self.local_fs_adapter.has_supported_archive_extension(path):
            log.info("Downloading archive %s", path)
            file_path = self._download_file(bucket_name, key)

            if not include_files:
                return self.local_fs_adapter.list_dirs(file_path)
            return self.local_fs_adapter.list_entries(file_path, max_file_size)

        if self._is_file(bucket_name, key):
            raise ValueError(f"Path corresponds to a file without a supported archive extension {path}")

        res = self._get_directory_entries(bucket_name, key, include_files=include_files, max_file_size=max_file_size)
        return res

    def list_entries(self, path: str, max_file_size: Optional[float] = None) -> List[str]:
        return self._list_entries(path, max_file_size=max_file_size)

    def list_dirs(self, path: str) -> List[str]:
        return self._list_entries(path, include_files=False)

    def delete_path(self, path: str):
        bucket_name, key = self._get_bucket_name_and_key(path)

        response: Dict[str, Any] = self._s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key)

        objects_metadata: List[Dict[str, Any]] = response.get("Contents", [])
        object_keys_to_delete: List[str] = [object_metadata["Key"] for object_metadata in objects_metadata]

        log.info("Starting to delete %d objects at %s", len(object_keys_to_delete), path)

        max_delete_batch_size: int = 1000
        for i in range(0, len(object_keys_to_delete), max_delete_batch_size):
            delete_batch_keys = [
                {"Key": object_key} for object_key in object_keys_to_delete[i : i + max_delete_batch_size]
            ]

            delete_response: Dict[str, Any] = self._s3_client.delete_objects(
                Bucket=bucket_name, Delete={"Objects": delete_batch_keys}
            )

            errors: List[Dict[str, Any]] = delete_response.get("Errors", [])
            if len(errors) > 0:
                for error in errors:
                    log.error(
                        "Failed to delete %s with code %s, message %s",
                        error["Key"],
                        error["Code"],
                        error["Message"],
                    )

                raise RuntimeError(f"Error occurred during deletion at {path}")

            deleted_object_keys = [deleted_object["Key"] for deleted_object in delete_response.get("Deleted", [])]
            delete_batch_keys_set = set(object_keys_to_delete[i : i + max_delete_batch_size])
            deleted_object_keys_set = set(deleted_object_keys)
            unrequested_deleted_keys = deleted_object_keys_set.difference(delete_batch_keys_set)
            if len(unrequested_deleted_keys) > 0:
                raise RuntimeError(f"The following keys were unexpectedly deleted: {unrequested_deleted_keys}")
            undeleted_keys = delete_batch_keys_set.difference(deleted_object_keys_set)
            if len(undeleted_keys) > 0:
                raise RuntimeError(f"The following keys failed to be deleted: {undeleted_keys}")

    def _is_file(self, bucket_name: str, key: str) -> bool:
        try:
            self._s3_client.head_object(Bucket=bucket_name, Key=key)
            return True
        except boto_exceptions.ClientError as e:
            if int(e.response["Error"]["Code"]) == 404:
                return False

            raise e

    def is_file(self, path: str) -> bool:
        bucket_name, key = self._get_bucket_name_and_key(path)

        return self._is_file(bucket_name, key)


class StorageCleaner:
    def __init__(
        self,
        dry_run: bool = False,
        ignore_prompts: bool = False,
        runs_require_checkpoint_dir: bool = True,
        r2_account_id: Optional[str] = None,
        max_archive_size: Optional[float] = None,
    ) -> None:
        self._dry_run: bool = dry_run
        self._runs_require_checkpoint_dir = runs_require_checkpoint_dir
        self._ignore_prompts: bool = ignore_prompts
        self._r2_account_id: Optional[str] = r2_account_id
        self._max_archive_size: Optional[float] = max_archive_size
        self._storage_adapters: Dict[StorageType, StorageAdapter] = {}

    def _get_storage_adapter(self, storage_type: StorageType) -> StorageAdapter:
        if storage_type not in self._storage_adapters:
            self._storage_adapters[storage_type] = StorageAdapter.create_storage_adapter(
                storage_type, self._r2_account_id
            )

        return self._storage_adapters[storage_type]

    def _get_storage_adapter_for_path(self, path: str) -> StorageAdapter:
        storage_type = StorageAdapter.get_storage_type_for_path(path)
        return self._get_storage_adapter(storage_type)

    @staticmethod
    def _contains_checkpoint_dir(dir_entries: List[str]) -> bool:
        return any(re.match(r"step\d+(-unsharded)?", entry) is not None for entry in dir_entries)

    @staticmethod
    def _contains_nontrivial_checkpoint_dir(dir_entries: List[str]) -> bool:
        return any(re.match(r"step[1-9]\d*(-unsharded)?", entry) is not None for entry in dir_entries)

    def _verify_deletion_without_checkpoint_dir(self, run_dir_or_archive: str, run_entries: List[str]) -> bool:
        msg = f"No checkpoint dir found in run directory entry {run_dir_or_archive} (first 5 entries: {run_entries[:5]}). This entry might not correspond to a run."
        if self._runs_require_checkpoint_dir:
            raise ValueError(msg)

        log.warning(msg)

        if not self._ignore_prompts:
            while True:
                response = input(f"{msg} Would you still like to delete {run_dir_or_archive}? (y/skip/exit) ")
                if response.lower() == "y":
                    return True
                elif response.lower() == "exit":
                    raise ValueError(msg)
                elif response.lower() == "skip":
                    return False

        return True

    def _delete_if_bad_run(self, storage: StorageAdapter, run_dir_or_archive: str):
        run_entries = storage.list_entries(run_dir_or_archive)

        should_delete = True
        if not self._contains_checkpoint_dir(run_entries):
            should_delete = self._verify_deletion_without_checkpoint_dir(run_dir_or_archive, run_entries)

        if should_delete and not self._contains_nontrivial_checkpoint_dir(run_entries):
            if self._dry_run:
                log.info("Would delete run directory or archive %s", run_dir_or_archive)
            else:
                log.info("Deleting run directory or archive %s", run_dir_or_archive)
                storage.delete_path(run_dir_or_archive)
        else:
            log.info("Skipping run directory or archive %s", run_dir_or_archive)

    def delete_bad_run(self, run_dir_or_archive: str):
        log.info("Starting deletion of bad run at %s", run_dir_or_archive)

        storage: StorageAdapter = self._get_storage_adapter_for_path(run_dir_or_archive)
        self._delete_if_bad_run(storage, run_dir_or_archive)

    def delete_bad_runs(self, runs_directory: str):
        log.info("Starting deletion of bad runs at %s", runs_directory)

        if not runs_directory.endswith("/"):
            raise ValueError(
                "Runs path does not end with '/'. Please verify that path is a directory and re-run with trailing '/'."
            )

        storage: StorageAdapter = self._get_storage_adapter_for_path(runs_directory)
        runs_dir_entries = [
            os.path.join(runs_directory, entry)
            for entry in storage.list_entries(runs_directory, max_file_size=self._max_archive_size)
        ]
        for runs_dir_entry in runs_dir_entries:
            self._delete_if_bad_run(storage, runs_dir_entry)


def perform_operation(args: argparse.Namespace):
    if args.dry_run:
        log.info("Dry run, no irreversible actions will be taken")

    if args.op == CleaningOperations.DELETE_BAD_RUNS:
        storage_cleaner = StorageCleaner(
            dry_run=args.dry_run,
            ignore_prompts=args.yes,
            runs_require_checkpoint_dir=args.runs_require_checkpoint_dir,
            r2_account_id=args.r2_account_id,
            max_archive_size=args.max_archive_size,
        )
        if args.runs_directory is not None:
            storage_cleaner.delete_bad_runs(args.runs_directory)
        elif args.run_path is not None:
            storage_cleaner.delete_bad_run(args.run_path)
        else:
            raise ValueError("Neither runs directory not run path provided for run cleaning")
    else:
        raise NotImplementedError(args.op)


def _add_delete_subparser(subparsers: _SubParsersAction):
    delete_runs_parser: ArgumentParser = subparsers.add_parser(
        "clean", help="Delete bad runs (e.g. runs with no non-trivial checkpoints)"
    )
    delete_runs_parser.set_defaults(op=CleaningOperations.DELETE_BAD_RUNS)

    path_parser = delete_runs_parser.add_mutually_exclusive_group(required=True)
    path_parser.add_argument(
        "--runs_directory",
        default=None,
        help="Path to directory containing one or more runs",
    )
    path_parser.add_argument(
        "--run_path",
        default=None,
        help="Path to directory or archive file corresponding to a run",
    )

    delete_runs_parser.add_argument(
        "--require_checkpoint_dir",
        action="store_true",
        dest="runs_require_checkpoint_dir",
        help="Enforces without prompt the sanity check that an entry being deleted has a checkpoint dir (and so is a run)",
    )
    delete_runs_parser.add_argument(
        "--max_archive_size",
        default=DEFAULT_MAX_ARCHIVE_SIZE,
        help="Max size archive files to consider for deletion (in bytes). Any archive larger than this is ignored/not deleted.",
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument(
        "-n",
        "--dry_run",
        action="store_true",
        help="If set, indicate actions but do not do them",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="If set, bypass prompts",
    )
    parser.add_argument(
        "--r2_account_id",
        default=R2_ACCOUNT_ID,
        help="Account id for R2 cloud storage",
    )

    subparsers = parser.add_subparsers(dest="command", help="Cleaning commands", required=True)
    _add_delete_subparser(subparsers)

    return parser


def main():
    args = get_parser().parse_args()

    util.prepare_cli_environment()
    perform_operation(args)


if __name__ == "__main__":
    main()
