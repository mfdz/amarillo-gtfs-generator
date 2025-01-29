from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Config(BaseSettings):
    env: str = 'DEV'
    stop_sources_file: str = 'data/stop_sources.json'
    max_age_carpool_offers_in_days: int = 180
    model_config = ConfigDict(extra='allow')

config = Config(_env_file='config', _env_file_encoding='utf-8')
