import json
from typing import Optional

from configuration_compiler.config_files_models.common.entity_definition_container_type import EntityDefinitionContainerTypeEnum
from configuration_compiler.config_files_models.common.entity_type_enum import EntityTypeEnum
from configuration_compiler.config_files_models.env.model import Entity, RawEnvConfigModel


class NoDataRootLocationException(Exception):
    pass


class EnvConfigModel(RawEnvConfigModel):
    @classmethod
    def from_str(cls, env_config: str) -> "EnvConfigModel":
        try:
            return cls(**json.loads(env_config))
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse environment config file: {e}") from e

    @property
    def is_source_storage_configured(self) -> bool:
        return bool(self._source_storage_entities_config)

    @property
    def _source_storage_entities_config(self) -> Optional[dict[str, Entity]]:
        if self.storage:
            if self.storage.source:
                return self.storage.source.entities
        return None

    @property
    def is_target_storage_configured(self) -> bool:
        return bool(self._target_storage_entities_config)

    @property
    def _target_storage_entities_config(self) -> Optional[dict[str, Entity]]:
        if self.storage:
            if self.storage.target:
                return self.storage.target.entities
        return None

    def data_root_location(self, entity_type: EntityTypeEnum) -> str:
        entities_config: dict[str, Entity] = self._entities_config(entity_type)
        try:
            return entities_config["default"].location
        except KeyError as ke:
            raise NoDataRootLocationException("Data location is not defined in the environment file") from ke

    def entity_specific_location(self, entity_id: str, entity_type: EntityTypeEnum) -> Optional[str]:
        entities_config: dict[str, Entity] = self._entities_config(entity_type)
        if entity_id in entities_config:
            return entities_config[entity_id].location
        return None

    def storage_data_format(self, entity_id: str, entity_type: EntityTypeEnum) -> str:
        entities_config = self._entities_config(entity_type)
        if entity_id in entities_config:
            return entities_config[entity_id].format
        if "default" in entities_config:
            return entities_config["default"].format
        raise EnvironmentError(f"Default file format type in storage for {entity_id} was not defined")

    def _entities_config(self, entity_type: EntityTypeEnum) -> dict[str, Entity]:
        if entity_type == EntityTypeEnum.SOURCE:
            config = self._source_storage_entities_config
            if config:
                return config
        elif entity_type == EntityTypeEnum.TARGET:
            config = self._target_storage_entities_config
            if config:
                return config
        raise ValueError(f"Invalid file format type was defined for: {entity_type}")

    @property
    def secondary_lake_location(self) -> str:
        return self.storage.secondary_lake.location

    @property
    def target_entities_container_type(self) -> EntityDefinitionContainerTypeEnum:
        if self.is_target_storage_configured:
            return EntityDefinitionContainerTypeEnum.STORAGE

        return EntityDefinitionContainerTypeEnum.METASTORE

    @property
    def source_entities_container_type(self) -> EntityDefinitionContainerTypeEnum:
        if self.is_source_storage_configured:
            return EntityDefinitionContainerTypeEnum.STORAGE
        return EntityDefinitionContainerTypeEnum.METASTORE
