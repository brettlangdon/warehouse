import ldclient
from ldclient.config import Config

def includeme(config):
    ldclient.set_config(Config(config.registery.settings["warehouse.launchdarkly.sdk_key"]))
