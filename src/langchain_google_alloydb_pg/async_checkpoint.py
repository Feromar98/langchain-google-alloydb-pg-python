# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from contextlib import asynccontextmanager
from typing import Any, Optional, Sequence, Tuple, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .engine import CHECKPOINT_WRITES_TABLE, CHECKPOINTS_TABLE, AlloyDBEngine


class AsyncAlloyDBSaver(BaseCheckpointSaver[str]):
    """Checkpoint stored in an AlloyDB for PostgreSQL database."""

    __create_key = object()

    jsonplus_serde = JsonPlusSerializer()

    def __init__(
        self,
        key: object,
        pool: AsyncEngine,
        table_name: str = CHECKPOINTS_TABLE,
        schema_name: str = "public",
        serde: Optional[SerializerProtocol] = None,
    ) -> None:
        super().__init__(serde=serde)
        if key != AsyncAlloyDBSaver.__create_key:
            raise Exception(
                "only create class through 'create' or 'create_sync' methods"
            )
        self.pool = pool
        self.table_name = table_name
        self.table_name_writes = f"{table_name}_writes"
        self.schema_name = schema_name

    @classmethod
    async def create(
        cls,
        engine: AlloyDBEngine,
        table_name: str = CHECKPOINTS_TABLE,
        schema_name: str = "public",
        serde: Optional[SerializerProtocol] = None,
    ) -> "AsyncAlloyDBSaver":
        """Create a new AsyncAlloyDBSaver instance.

        Args:
            engine (AlloyDBEngine): AlloyDB engine to use.
            schema_name (str): The schema name where the table is located (default: "public").
            serde (SerializerProtocol): Serializer for encoding/decoding checkpoints (default: None).

        Raises:
            IndexError: If the table provided does not contain required schema.

        Returns:
            AsyncAlloyDBSaver: A newly created instance of AsyncAlloyDBSaver.
        """

        checkpoints_table_schema = await engine._aload_table_schema(
            table_name, schema_name
        )
        checkpoints_column_names = checkpoints_table_schema.columns.keys()

        checkpoints_required_columns = [
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "parent_checkpoint_id",
            "type",
            "checkpoint",
            "metadata",
        ]

        if not (
            all(x in checkpoints_column_names for x in checkpoints_required_columns)
        ):
            raise IndexError(
                f"Table checkpoints.'{schema_name}' has incorrect schema. Got "
                f"column names '{checkpoints_column_names}' but required column names "
                f"'{checkpoints_required_columns}'.\nPlease create table with following schema:"
                f"\nCREATE TABLE {schema_name}.checkpoints ("
                "\n    thread_id TEXT NOT NULL,"
                "\n    checkpoint_ns TEXT NOT NULL,"
                "\n    checkpoint_id TEXT NOT NULL,"
                "\n    parent_checkpoint_id TEXT,"
                "\n    type TEXT,"
                "\n    checkpoint JSONB NOT NULL,"
                "\n    metadata JSONB NOT NULL"
                "\n);"
            )

        checkpoint_writes_table_schema = await engine._aload_table_schema(
            f"{table_name}_writes", schema_name
        )
        checkpoint_writes_column_names = checkpoint_writes_table_schema.columns.keys()

        checkpoint_writes_columns = [
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            "channel",
            "type",
            "blob",
        ]

        if not (
            all(x in checkpoint_writes_column_names for x in checkpoint_writes_columns)
        ):
            raise IndexError(
                f"Table checkpoint_writes.'{schema_name}' has incorrect schema. Got "
                f"column names '{checkpoint_writes_column_names}' but required column names "
                f"'{checkpoint_writes_columns}'.\nPlease create table with following schema:"
                f"\nCREATE TABLE {schema_name}.checkpoint_writes ("
                "\n    thread_id TEXT NOT NULL,"
                "\n    checkpoint_ns TEXT NOT NULL,"
                "\n    checkpoint_id TEXT NOT NULL,"
                "\n    task_id TEXT NOT NULL,"
                "\n    idx INT NOT NULL,"
                "\n    channel TEXT NOT NULL,"
                "\n    type TEXT,"
                "\n    blob JSONB NOT NULL"
                "\n);"
            )
        return cls(cls.__create_key, engine._pool, table_name, schema_name, serde)

    def _dump_checkpoint(self, checkpoint: Checkpoint) -> str:
        checkpoint["pending_sends"] = []
        return json.dumps(checkpoint)

    def _dump_metadata(self, metadata: CheckpointMetadata) -> str:
        serialized_metadata = self.jsonplus_serde.dumps(metadata)
        # NOTE: we're using JSON serializer (not msgpack), so we need to remove null characters before writing
        return serialized_metadata.decode().replace("\\u0000", "")

    def _dump_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        task_path: str,
        writes: Sequence[tuple[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "task_path": task_path,
                "idx": WRITES_IDX_MAP.get(channel, idx),
                "channel": channel,
                "type": self.serde.dumps_typed(value)[0],
                "blob": self.serde.dumps_typed(value)[1],
            }
            for idx, (channel, value) in enumerate(writes)
        ]

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronously store a checkpoint with its configuration and metadata.

        Args:
            config (RunnableConfig): Configuration for the checkpoint.
            checkpoint (Checkpoint): The checkpoint to store.
            metadata (CheckpointMetadata): Additional metadata for the checkpoint.
            new_versions (ChannelVersions): New channel versions as of this write.

        Returns:
            RunnableConfig: Updated configuration after storing the checkpoint.
        """
        configurable = config["configurable"].copy()
        thread_id = configurable.pop("thread_id")
        checkpoint_ns = configurable.pop("checkpoint_ns")
        checkpoint_id = configurable.pop(
            "checkpoint_id", configurable.pop("thread_ts", None)
        )

        copy = checkpoint.copy()
        next_config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

        query = f"""INSERT INTO "{self.schema_name}"."{self.table_name}" (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata)
                    VALUES (:thread_id, :checkpoint_ns, :checkpoint_id, :parent_checkpoint_id, :checkpoint, :metadata)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id)
                    DO UPDATE SET
                        checkpoint = EXCLUDED.checkpoint,
                        metadata = EXCLUDED.metadata;
            """

        async with self.pool.connect() as conn:
            await conn.execute(
                text(query),
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint["id"],
                    "parent_checkpoint_id": checkpoint_id,
                    "checkpoint": self._dump_checkpoint(copy),
                    "metadata": self._dump_metadata(metadata),
                },
            )
            await conn.commit()

        return next_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronously store intermediate writes linked to a checkpoint.
        Args:
            config (RunnableConfig): Configuration of the related checkpoint.
            writes (List[Tuple[str, Any]]): List of writes to store.
            task_id (str): Identifier for the task creating the writes.
            task_path (str): Path of the task creating the writes.

            Returns:
                None
        """
        upsert = f"""INSERT INTO "{self.schema_name}"."{self.table_name_writes}"(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
                    VALUES (:thread_id, :checkpoint_ns, :checkpoint_id, :task_id, :idx, :channel, :type, :blob)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO UPDATE SET
                    channel = EXCLUDED.channel,
                        type = EXCLUDED.type,
                        blob = EXCLUDED.blob;
                """
        insert = f"""INSERT INTO "{self.schema_name}"."{self.table_name_writes}"(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
                    VALUES (:thread_id, :checkpoint_ns, :checkpoint_id, :task_id, :idx, :channel, :type, :blob)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO NOTHING
                """
        query = upsert if all(w[0] in WRITES_IDX_MAP for w in writes) else insert

        params = self._dump_writes(
            config["configurable"]["thread_id"],
            config["configurable"]["checkpoint_ns"],
            config["configurable"]["checkpoint_id"],
            task_id,
            task_path,
            writes,
        )

        async with self.pool.connect() as conn:
            await conn.execute(
                text(query),
                params,
            )
            await conn.commit()
