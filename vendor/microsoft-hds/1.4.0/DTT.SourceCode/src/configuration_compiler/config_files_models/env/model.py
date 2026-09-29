from enum import Enum
from typing import Optional

from configuration_compiler.config_files_models.models_base import NoExtrasBaseModel
from pydantic import Field


class FormatEnum(str, Enum):
    csv = "csv"
    parquet = "parquet"
    delta = "delta"


class Entity(NoExtrasBaseModel):
    location: str
    format: FormatEnum


class StorageConfiguration(NoExtrasBaseModel):
    entities: dict[str, Entity] = Field(
        ...,
        description="PA collection of entities by their names (can be tables or folders). "
        "The key 'default' is reserved for the default entity. The default entity is "
        "used when no entity is specified in the source or target configuration.",
    )


class SecondaryLake(NoExtrasBaseModel):
    location: str


class Storage(NoExtrasBaseModel):
    source: Optional[StorageConfiguration] = Field(
        None, description="The source storage configuration for the environment"
    )
    target: Optional[StorageConfiguration] = Field(
        None, description="The target storage configuration for the environment"
    )
    secondary_lake: SecondaryLake = Field(..., description="The secondary lake configuration for the environment")


class RawEnvConfigModel(NoExtrasBaseModel):
    storage: Storage = Field(..., description="The source storage configuration for the environment")
