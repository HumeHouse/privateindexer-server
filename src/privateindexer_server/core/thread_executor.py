from concurrent.futures.process import ProcessPoolExecutor

from privateindexer_server.core.config import MAX_THREADS

EXECUTOR = ProcessPoolExecutor(max_workers=MAX_THREADS)
