from concurrent.futures.process import ProcessPoolExecutor

from privateindexer_server.core.config import MAX_THREADS
from privateindexer_server.core.logger import log

_hash_executor: ProcessPoolExecutor | None = None


def get_hash_executor() -> ProcessPoolExecutor:
    """
    Create or return an existing process pool executor for hashing files
    """
    global _hash_executor
    if _hash_executor is None or _hash_executor._shutdown_thread:
        _hash_executor = ProcessPoolExecutor(max_workers=MAX_THREADS)
        log.debug("[EXECUTOR] Spawned new hash executor")
    return _hash_executor
