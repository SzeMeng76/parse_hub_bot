from repo.settings.schema import Config


def migrate(config: Config) -> Config:
    return config.model_copy(update={"schema_version": 2, "auto_delete_url": False})
