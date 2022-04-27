import logging
import itertools
from typing import Any, Callable, Dict, Optional, List, Union, Iterator, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import pyarrow

import ray
from ray.types import ObjectRef
from ray.data.block import Block, BlockAccessor
from ray.data.context import DatasetContext
from ray.data.datasource.datasource import ReadTask
from ray.data.datasource.file_based_datasource import (
    FileBasedDatasource,
    _resolve_paths_and_filesystem,
    _resolve_kwargs,
)
from ray.data.impl.block_list import BlockMetadata
from ray.data.impl.output_buffer import BlockOutputBuffer
from ray.data.impl.progress_bar import ProgressBar
from ray.data.impl.remote_fn import cached_remote_fn
from ray.data.impl.util import _check_pyarrow_version

logger = logging.getLogger(__name__)

PIECES_PER_META_FETCH = 18
PARALLELIZE_META_FETCH_THRESHOLD = 24

# The number of rows to read per batch. This is sized to generate 10MiB batches
# for rows about 1KiB in size.
PARQUET_READER_ROW_BATCH_SIZE = 100000


def _register_parquet_file_fragment_serialization():
    from pyarrow.dataset import ParquetFileFragment

    def serialize(frag):
        return (frag.format, frag.path, frag.filesystem, frag.partition_expression)

    def deserialize(obj):
        file_format, path, filesystem, partition_expression = obj
        return file_format.make_fragment(path, filesystem, partition_expression)

    ray.util.register_serializer(
        ParquetFileFragment, serializer=serialize, deserializer=deserialize
    )


def _deregister_parquet_file_fragment_serialization():
    from pyarrow.dataset import ParquetFileFragment

    ray.util.deregister_serializer(ParquetFileFragment)


class ParquetDatasource(FileBasedDatasource):
    """Parquet datasource, for reading and writing Parquet files.

    Examples:
        >>> source = ParquetDatasource()
        >>> ray.data.read_datasource(source, paths="/path/to/dir").take()
        ... [{"a": 1, "b": "foo"}, ...]
    """

    def prepare_read(
        self,
        parallelism: int,
        paths: Union[str, List[str]],
        filesystem: Optional["pyarrow.fs.FileSystem"] = None,
        columns: Optional[List[str]] = None,
        schema: Optional[Union[type, "pyarrow.lib.Schema"]] = None,
        _block_udf: Optional[Callable[[Block], Block]] = None,
        **reader_args,
    ) -> List[ReadTask]:
        """Creates and returns read tasks for a Parquet file-based datasource."""
        # NOTE: We override the base class FileBasedDatasource.prepare_read
        # method in order to leverage pyarrow's ParquetDataset abstraction,
        # which simplifies partitioning logic. We still use
        # FileBasedDatasource's write side (do_write), however.
        _check_pyarrow_version()
        from ray import cloudpickle
        import pyarrow as pa
        import pyarrow.parquet as pq
        import numpy as np

        paths, filesystem = _resolve_paths_and_filesystem(paths, filesystem)
        if len(paths) == 1:
            paths = paths[0]

        dataset_kwargs = reader_args.pop("dataset_kwargs", {})
        pq_ds = pq.ParquetDataset(
            paths, **dataset_kwargs, filesystem=filesystem, use_legacy_dataset=False
        )
        if schema is None:
            schema = pq_ds.schema
        if columns:
            schema = pa.schema(
                [schema.field(column) for column in columns], schema.metadata
            )

        def read_pieces(serialized_pieces: str) -> Iterator[pa.Table]:
            # Implicitly trigger S3 subsystem initialization by importing
            # pyarrow.fs.
            import pyarrow.fs  # noqa: F401

            # Deserialize after loading the filesystem class.
            try:
                _register_parquet_file_fragment_serialization()
                pieces: List[
                    "pyarrow._dataset.ParquetFileFragment"
                ] = cloudpickle.loads(serialized_pieces)
            finally:
                _deregister_parquet_file_fragment_serialization()

            # Ensure that we're reading at least one dataset fragment.
            assert len(pieces) > 0

            from pyarrow.dataset import _get_partition_keys

            ctx = DatasetContext.get_current()
            output_buffer = BlockOutputBuffer(
                block_udf=_block_udf, target_max_block_size=ctx.target_max_block_size
            )

            logger.debug(f"Reading {len(pieces)} parquet pieces")
            use_threads = reader_args.pop("use_threads", False)
            for piece in pieces:
                part = _get_partition_keys(piece.partition_expression)
                batches = piece.to_batches(
                    use_threads=use_threads,
                    columns=columns,
                    schema=schema,
                    batch_size=PARQUET_READER_ROW_BATCH_SIZE,
                    **reader_args,
                )
                for batch in batches:
                    table = pyarrow.Table.from_batches([batch], schema=schema)
                    if part:
                        for col, value in part.items():
                            table = table.set_column(
                                table.schema.get_field_index(col),
                                col,
                                pa.array([value] * len(table)),
                            )
                    # If the table is empty, drop it.
                    if table.num_rows > 0:
                        output_buffer.add_block(table)
                        if output_buffer.has_next():
                            yield output_buffer.next()
            output_buffer.finalize()
            if output_buffer.has_next():
                yield output_buffer.next()

        if _block_udf is not None:
            # Try to infer dataset schema by passing dummy table through UDF.
            dummy_table = schema.empty_table()
            try:
                inferred_schema = _block_udf(dummy_table).schema
                inferred_schema = inferred_schema.with_metadata(schema.metadata)
            except Exception:
                logger.debug(
                    "Failed to infer schema of dataset by passing dummy table "
                    "through UDF due to the following exception:",
                    exc_info=True,
                )
                inferred_schema = schema
        else:
            inferred_schema = schema
        read_tasks = []
        if len(pq_ds.pieces) > PARALLELIZE_META_FETCH_THRESHOLD:
            metadata = _fetch_metadata_remotely(pq_ds.pieces)
        else:
            metadata = _fetch_metadata(pq_ds.pieces)

        try:
            _register_parquet_file_fragment_serialization()
            for piece_data in np.array_split(
                list(zip(pq_ds.pieces, metadata)), parallelism
            ):
                if len(piece_data) == 0:
                    continue
                pieces, metadata = zip(*piece_data)
                serialized_pieces = cloudpickle.dumps(pieces)
                meta = _build_block_metadata(pieces, metadata, inferred_schema)
                read_tasks.append(
                    ReadTask(lambda p=serialized_pieces: read_pieces(p), meta)
                )
        finally:
            _deregister_parquet_file_fragment_serialization()

        return read_tasks

    def _write_block(
        self,
        f: "pyarrow.NativeFile",
        block: BlockAccessor,
        writer_args_fn: Callable[[], Dict[str, Any]] = lambda: {},
        **writer_args,
    ):
        import pyarrow.parquet as pq

        writer_args = _resolve_kwargs(writer_args_fn, **writer_args)
        pq.write_table(block.to_arrow(), f, **writer_args)

    def _file_format(self) -> str:
        return "parquet"


def _fetch_metadata_remotely(
    pieces: List["pyarrow._dataset.ParquetFileFragment"],
) -> List[ObjectRef["pyarrow.parquet.FileMetaData"]]:
    from ray import cloudpickle

    remote_fetch_metadata = cached_remote_fn(_fetch_metadata_serialization_wrapper)
    metas = []
    parallelism = min(len(pieces) // PIECES_PER_META_FETCH, 100)
    meta_fetch_bar = ProgressBar("Metadata Fetch Progress", total=parallelism)
    try:
        _register_parquet_file_fragment_serialization()
        for pcs in np.array_split(pieces, parallelism):
            if len(pcs) == 0:
                continue
            metas.append(remote_fetch_metadata.remote(cloudpickle.dumps(pcs)))
    finally:
        _deregister_parquet_file_fragment_serialization()
    metas = meta_fetch_bar.fetch_until_complete(metas)
    return list(itertools.chain.from_iterable(metas))


def _fetch_metadata_serialization_wrapper(
    pieces: str,
) -> List["pyarrow.parquet.FileMetaData"]:
    # Implicitly trigger S3 subsystem initialization by importing
    # pyarrow.fs.
    import pyarrow.fs  # noqa: F401
    from ray import cloudpickle

    # Deserialize after loading the filesystem class.
    try:
        _register_parquet_file_fragment_serialization()
        pieces: List["pyarrow._dataset.ParquetFileFragment"] = cloudpickle.loads(pieces)
    finally:
        _deregister_parquet_file_fragment_serialization()

    return _fetch_metadata(pieces)


def _fetch_metadata(
    pieces: List["pyarrow.dataset.ParquetFileFragment"],
) -> List["pyarrow.parquet.FileMetaData"]:
    piece_metadata = []
    for p in pieces:
        try:
            piece_metadata.append(p.metadata)
        except AttributeError:
            break
    return piece_metadata


def _build_block_metadata(
    pieces: List["pyarrow.dataset.ParquetFileFragment"],
    metadata: List["pyarrow.parquet.FileMetaData"],
    schema: Optional[Union[type, "pyarrow.lib.Schema"]],
) -> BlockMetadata:
    input_files = [p.path for p in pieces]
    if len(metadata) == len(pieces):
        # Piece metadata was available, construct a normal
        # BlockMetadata.
        block_metadata = BlockMetadata(
            num_rows=sum(m.num_rows for m in metadata),
            size_bytes=sum(
                sum(m.row_group(i).total_byte_size for i in range(m.num_row_groups))
                for m in metadata
            ),
            schema=schema,
            input_files=input_files,
            exec_stats=None,
        )  # Exec stats filled in later.
    else:
        # Piece metadata was not available, construct an empty
        # BlockMetadata.
        block_metadata = BlockMetadata(
            num_rows=None, size_bytes=None, schema=schema, input_files=input_files
        )
    return block_metadata
