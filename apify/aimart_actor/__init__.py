"""AImart Parquet as an Apify Actor: messy files in, typed Parquet and SQL answers out.

``core`` holds the work and knows nothing of the Apify SDK, so the repository's test suite runs
it without the SDK installed; ``actor`` binds it to the SDK (input, storages, charging).
"""
